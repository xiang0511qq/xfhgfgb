import base64, hashlib, json, mimetypes, os, threading, time, tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = 'https://pc-api.uc.cn/1/clouddrive'
ORIGIN = 'https://drive.uc.cn'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'

class UCClient:
    def __init__(self, cookie, log, progress):
        self.cookie = cookie.strip()
        self.log = log
        self.progress = progress
        self.headers = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*',
                        'Origin': ORIGIN, 'Referer': ORIGIN + '/', 'Cookie': self.cookie}

    def request_json(self, method, path, data=None):
        query = urlencode({'pr': 'UCBrowser', 'fr': 'pc'})
        url = f'{API}{path}?{query}'
        body = None if data is None else json.dumps(data).encode()
        headers = dict(self.headers)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=60) as r:
                result = json.loads(r.read().decode('utf-8', 'replace'))
        except (HTTPError, URLError, TimeoutError) as e:
            raise RuntimeError(f'接口请求失败: {e}')
        if result.get('code', 0) not in (0, '0') and result.get('status', 200) not in (200, '200'):
            raise RuntimeError(f'接口返回错误: {result}')
        return result

    def upload(self, path, parent_id):
        size = os.path.getsize(path)
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        self.log(f'准备上传: {name} ({size / 1024 / 1024:.2f} MB)')
        md5 = hashlib.md5(); sha1 = hashlib.sha1()
        with open(path, 'rb') as f:
            while chunk := f.read(8 * 1024 * 1024):
                md5.update(chunk); sha1.update(chunk)
        now = int(time.time() * 1000)
        pre = self.request_json('POST', '/file/upload/pre', {
            'ccp_hash_update': True, 'parallel_upload': True, 'pdir_fid': parent_id,
            'dir_name': '', 'file_name': name, 'format_type': mime,
            'l_created_at': now, 'l_updated_at': now, 'size': size
        })
        info = pre.get('data') or {}
        task = info.get('task_id')
        if not task: raise RuntimeError(f'预上传失败: {pre}')
        self.log('预上传成功，正在校验哈希')
        hashed = self.request_json('POST', '/file/update/hash', {'task_id': task, 'md5': md5.hexdigest(), 'sha1': sha1.hexdigest()})
        if (hashed.get('data') or {}).get('finish'):
            self.progress(100); self.log('秒传成功'); return
        self._upload_parts(path, mime, info)
        self.request_json('POST', '/file/upload/finish', {'task_id': task, 'obj_key': info.get('obj_key')})
        self.progress(100); self.log('上传完成')

    def _upload_parts(self, path, mime, info):
        bucket, obj_key, upload_id = info['bucket'], info['obj_key'], info['upload_id']
        auth_info, task = info['auth_info'], info['task_id']
        part_size = int((info.get('metadata') or {}).get('part_size') or 4 * 1024 * 1024)
        base = info.get('upload_url', '')
        if not base: raise RuntimeError('接口没有返回 upload_url')
        if not base.startswith('http'): base = 'https://' + base
        base = base.rstrip('/')
        if bucket + '.' not in base.split('/')[2]:
            host = base.split('/')[2]
            base = base.replace(host, bucket + '.' + host, 1)
        oss = base + '/' + obj_key.lstrip('/')
        total = max(1, (os.path.getsize(path) + part_size - 1) // part_size)
        etags = []
        with open(path, 'rb') as f:
            for number in range(1, total + 1):
                block = f.read(part_size)
                date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())
                meta = f'PUT\n\n{mime}\n{date}\nx-oss-date:{date}\nx-oss-user-agent:aliyun-sdk-js/6.6.1\n/{bucket}/{obj_key}?partNumber={number}&uploadId={upload_id}'
                auth = self.request_json('POST', '/file/upload/auth', {'task_id': task, 'auth_info': auth_info, 'auth_meta': meta})
                key = (auth.get('data') or {}).get('auth_key')
                if not key: raise RuntimeError(f'获取分片授权失败: {auth}')
                q = urlencode({'partNumber': number, 'uploadId': upload_id})
                req = Request(oss + '?' + q, data=block, method='PUT', headers={
                    'User-Agent': UA, 'Content-Type': mime, 'Authorization': key,
                    'x-oss-date': date, 'x-oss-user-agent': 'aliyun-sdk-js/6.6.1'})
                with urlopen(req, timeout=180) as response:
                    etags.append(response.headers.get('ETag', '').strip('"'))
                self.progress(number * 100 / total); self.log(f'已上传分片 {number}/{total}')
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n' + ''.join(f'<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>\n' for i,e in enumerate(etags, 1)) + '</CompleteMultipartUpload>'
        md5 = base64.b64encode(hashlib.md5(xml.encode()).digest()).decode()
        callback = info.get('callback') or {}
        callback64 = base64.b64encode(json.dumps(callback, separators=(',', ':')).encode()).decode()
        date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())
        meta = f'POST\n{md5}\napplication/xml\n{date}\nx-oss-callback:{callback64}\nx-oss-date:{date}\nx-oss-user-agent:aliyun-sdk-js/6.6.1\n/{bucket}/{obj_key}?uploadId={upload_id}'
        auth = self.request_json('POST', '/file/upload/auth', {'task_id': task, 'auth_info': auth_info, 'auth_meta': meta})
        key = (auth.get('data') or {}).get('auth_key')
        req = Request(oss + '?uploadId=' + upload_id, data=xml.encode(), method='POST', headers={
            'User-Agent': UA, 'Content-Type': 'application/xml', 'Authorization': key,
            'Content-MD5': md5, 'x-oss-callback': callback64, 'x-oss-date': date,
            'x-oss-user-agent': 'aliyun-sdk-js/6.6.1'})
        with urlopen(req, timeout=180): pass

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('UC网盘文件上传'); self.geometry('620x520'); self.resizable(False, False)
        self.file = tk.StringVar(); self.parent = tk.StringVar(value='0')
        ttk.Label(self, text='UC网盘 Cookie（只在本机使用）').pack(anchor='w', padx=14, pady=(12,3))
        self.cookie = tk.Text(self, height=4, width=78); self.cookie.pack(padx=14)
        row = ttk.Frame(self); row.pack(fill='x', padx=14, pady=10)
        ttk.Label(row, text='目标文件夹 ID').pack(side='left'); ttk.Entry(row, textvariable=self.parent, width=24).pack(side='left', padx=8)
        ttk.Button(self, text='选择文件', command=self.choose).pack(anchor='w', padx=14)
        ttk.Entry(self, textvariable=self.file, width=78).pack(padx=14, pady=6)
        self.bar = ttk.Progressbar(self, length=590, mode='determinate'); self.bar.pack(padx=14, pady=8)
        self.start = ttk.Button(self, text='开始上传', command=self.start_upload); self.start.pack(pady=4)
        self.logbox = tk.Text(self, height=14, width=78, state='disabled'); self.logbox.pack(padx=14, pady=8)
    def choose(self):
        p = filedialog.askopenfilename();
        if p: self.file.set(p)
    def log(self, s): self.after(0, lambda: (self.logbox.configure(state='normal'), self.logbox.insert('end', s+'\n'), self.logbox.see('end'), self.logbox.configure(state='disabled')))
    def progress(self, n): self.after(0, lambda: self.bar.configure(value=n))
    def start_upload(self):
        cookie = self.cookie.get('1.0','end').strip(); path = self.file.get().strip(); parent = self.parent.get().strip() or '0'
        if not cookie or not path: return messagebox.showwarning('提示','请填写 Cookie 并选择文件')
        self.start.configure(state='disabled'); self.bar['value'] = 0
        def run():
            try: UCClient(cookie, self.log, self.progress).upload(path, parent)
            except Exception as e: self.log('失败: ' + str(e)); self.after(0, lambda: messagebox.showerror('上传失败', str(e)))
            finally: self.after(0, lambda: self.start.configure(state='normal'))
        threading.Thread(target=run, daemon=True).start()

if __name__ == '__main__': App().mainloop()
