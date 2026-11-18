import base64, hashlib, json, mimetypes, os, struct, threading, time, tkinter as tk
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

    def request_json(self, method, path, data=None, params=None):
        query_data = {'pr': 'UCBrowser', 'fr': 'pc'}
        if params: query_data.update(params)
        query = urlencode(query_data)
        url = f'{API}{path}?{query}'
        body = None if data is None else json.dumps(data).encode()
        headers = dict(self.headers)
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=60) as r:
                result = json.loads(r.read().decode('utf-8', 'replace'))
        except HTTPError as e:
            try: detail = e.read().decode('utf-8', 'replace')
            except Exception: detail = ''
            raise RuntimeError(f'接口请求失败: HTTP {e.code} {detail[:500]}')
        except (URLError, TimeoutError) as e:
            raise RuntimeError(f'接口请求失败: {e}')
        if result.get('code', 0) not in (0, '0') and result.get('status', 200) not in (200, '200'):
            raise RuntimeError(f'接口返回错误: {result}')
        return result

    def upload(self, path, parent_id, make_share=True):
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
            self.progress(100); self.log('秒传成功')
            fid = self._extract_fid(hashed) or self._find_uploaded_fid(parent_id, name)
            if fid and make_share:
                link = self.create_share(fid, name)
                self.log('永久分享链接: ' + link)
                return link
            return fid
        self._upload_parts(path, mime, info)
        finished = self.request_json('POST', '/file/upload/finish', {'task_id': task, 'obj_key': info.get('obj_key')})
        self.progress(100); self.log('上传完成')
        fid = self._extract_fid(finished)
        if not fid:
            fid = self._find_uploaded_fid(parent_id, name)
        if fid and make_share:
            link = self.create_share(fid, name)
            self.log('永久分享链接: ' + link)
            return link
        return fid

    def create_folder(self, parent_id, name):
        result = self.request_json('POST', '/file', {'pdir_fid': parent_id, 'file_name': name, 'dir_init_lock': False})
        data = result.get('data') or {}
        fid = data.get('fid') or (data.get('file') or {}).get('fid')
        if not fid:
            raise RuntimeError(f'创建文件夹失败: {result}')
        self.log('已创建文件夹: ' + name)
        return fid

    def upload_folder(self, folder, parent_id):
        folder = os.path.abspath(folder)
        root_name = os.path.basename(folder.rstrip(os.sep)) or '上传文件夹'
        root_fid = self.create_folder(parent_id, root_name)
        folders = {folder: root_fid}
        files = []
        for current, dirs, names in os.walk(folder):
            dirs.sort(); names.sort()
            for d in dirs:
                child = os.path.join(current, d)
                folders[child] = self.create_folder(folders[current], d)
            for name in names:
                files.append(os.path.join(current, name))
        total = len(files)
        self.log(f'文件夹共 {total} 个文件，开始上传')
        for index, path in enumerate(files, 1):
            self.log(f'[{index}/{total}] ' + os.path.basename(path))
            self.upload(path, folders[os.path.dirname(path)], make_share=False)
        link = self.create_share(root_fid, root_name)
        self.log('永久文件夹分享链接: ' + link)
        return link

    def _extract_fid(self, result):
        data = result.get('data') or {}
        for obj in (data, data.get('file') or {}, data.get('task_resp') or {}, (data.get('task_resp') or {}).get('data') or {}):
            if obj.get('fid'):
                return obj['fid']
        return None

    def _find_uploaded_fid(self, parent_id, name):
        result = self.request_json('GET', '/file/sort', None)
        items = (result.get('data') or {}).get('list') or []
        for item in items:
            if item.get('pdir_fid') == parent_id and item.get('file_name') == name:
                return item.get('fid')
        return None

    def create_share(self, fid, title):
        result = self.request_json('POST', '/share', {'fid_list': [fid], 'title': title, 'url_type': 1, 'expired_type': 1})
        data = result.get('data') or {}
        task_resp = data.get('task_resp') or {}
        task_data = task_resp.get('data') or {}
        share_id = data.get('share_id') or task_data.get('share_id')
        task_id = data.get('task_id') or task_data.get('task_id')
        if not share_id and task_id:
            self.log('分享任务已创建，等待 UC 返回分享ID')
            for retry in range(12):
                time.sleep(0.6)
                task = self.request_json('GET', '/task', params={'task_id': task_id, 'retry_index': retry})
                td = task.get('data') or {}
                share_id = td.get('share_id') or ((td.get('task_resp') or {}).get('data') or {}).get('share_id')
                if share_id: break
        if not share_id:
            raise RuntimeError('分享创建成功但没有返回分享ID')
        detail = self.request_json('POST', '/share/password', {'share_id': share_id})
        d = detail.get('data') or {}
        url = d.get('share_url')
        if not url:
            raise RuntimeError('没有获取到分享链接')
        return url

    def _hash_contexts(self, path, part_size):
        # 计算 OSS/PDS 要求的 SHA1 中间状态。第一个分片不带 ctx，后续分片使用前一分片结束状态。
        h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
        tail = b''; contexts = []; total = 0; next_boundary = part_size
        def process(block):
            nonlocal h
            w = list(struct.unpack('>16I', block))
            for i in range(16, 80):
                v = w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16]
                w.append(((v << 1) | (v >> 31)) & 0xffffffff)
            a,b,c,d,e = h
            for i in range(80):
                if i < 20: f = (b & c) | ((~b) & d); k = 0x5A827999
                elif i < 40: f = b ^ c ^ d; k = 0x6ED9EBA1
                elif i < 60: f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC
                else: f = b ^ c ^ d; k = 0xCA62C1D6
                t = (((a << 5) | (a >> 27)) + f + e + k + w[i]) & 0xffffffff
                e,d,c,b,a = d,c,((b << 30) | (b >> 2)) & 0xffffffff,a,t
            h = [(h[0]+a)&0xffffffff,(h[1]+b)&0xffffffff,(h[2]+c)&0xffffffff,(h[3]+d)&0xffffffff,(h[4]+e)&0xffffffff]
        with open(path, 'rb') as f:
            while chunk := f.read(1024 * 1024):
                total += len(chunk)
                data = tail + chunk
                full = len(data) - (len(data) % 64)
                for pos in range(0, full, 64): process(data[pos:pos+64])
                tail = data[full:]
                while total >= next_boundary:
                    obj = {'hash_type':'sha1','h0':str(h[0]),'h1':str(h[1]),'h2':str(h[2]),'h3':str(h[3]),'h4':str(h[4]),'Nl':str(next_boundary*8),'Nh':'0','data':'','num':'0'}
                    contexts.append(base64.b64encode(json.dumps(obj,separators=(',',':')).encode()).decode())
                    next_boundary += part_size
        return contexts

    def _upload_parts(self, path, mime, info):
        bucket, obj_key, upload_id = info['bucket'], info['obj_key'], info['upload_id']
        auth_info, task = info['auth_info'], info['task_id']
        part_size = max(int((info.get('metadata') or {}).get('part_size') or 8 * 1024 * 1024), 8 * 1024 * 1024)
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
        hash_contexts = self._hash_contexts(path, part_size)
        with open(path, 'rb') as f:
            for number in range(1, total + 1):
                block = f.read(part_size)
                date = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())
                hash_ctx = hash_contexts[number - 2] if number >= 2 and number - 2 < len(hash_contexts) else ''
                hash_line = f'x-oss-hash-ctx:{hash_ctx}\n' if hash_ctx else ''
                meta = f'PUT\n\n{mime}\n{date}\nx-oss-date:{date}\n{hash_line}x-oss-user-agent:aliyun-sdk-js/6.6.1\n/{bucket}/{obj_key}?partNumber={number}&uploadId={upload_id}'
                auth = self.request_json('POST', '/file/upload/auth', {'task_id': task, 'auth_info': auth_info, 'auth_meta': meta})
                key = (auth.get('data') or {}).get('auth_key')
                if not key: raise RuntimeError(f'获取分片授权失败: {auth}')
                q = urlencode({'partNumber': number, 'uploadId': upload_id})
                req = Request(oss + '?' + q, data=block, method='PUT', headers={
                    'User-Agent': UA, 'Content-Type': mime, 'Authorization': key,
                    'x-oss-date': date, 'x-oss-user-agent': 'aliyun-sdk-js/6.6.1', **({'X-Oss-Hash-Ctx': hash_ctx} if hash_ctx else {})})
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
        self.file = tk.StringVar(); self.parent = tk.StringVar(value='0'); self.mode = tk.StringVar(value='file')
        ttk.Label(self, text='UC网盘 Cookie（只在本机使用）').pack(anchor='w', padx=14, pady=(12,3))
        self.cookie = tk.Text(self, height=4, width=78); self.cookie.pack(padx=14)
        row = ttk.Frame(self); row.pack(fill='x', padx=14, pady=10)
        ttk.Label(row, text='目标文件夹 ID').pack(side='left'); ttk.Entry(row, textvariable=self.parent, width=24).pack(side='left', padx=8)
        buttons = ttk.Frame(self); buttons.pack(anchor='w', padx=14)
        ttk.Button(buttons, text='选择文件', command=self.choose).pack(side='left')
        ttk.Button(buttons, text='选择文件夹', command=self.choose_folder).pack(side='left', padx=8)
        ttk.Entry(self, textvariable=self.file, width=78).pack(padx=14, pady=6)
        self.bar = ttk.Progressbar(self, length=590, mode='determinate'); self.bar.pack(padx=14, pady=8)
        self.start = ttk.Button(self, text='开始上传', command=self.start_upload); self.start.pack(pady=4)
        self.logbox = tk.Text(self, height=14, width=78, state='disabled'); self.logbox.pack(padx=14, pady=8)
    def choose(self):
        p = filedialog.askopenfilename();
        if p: self.file.set(p); self.mode.set('file')
    def choose_folder(self):
        p = filedialog.askdirectory();
        if p: self.file.set(p); self.mode.set('folder')
    def log(self, s): self.after(0, lambda: (self.logbox.configure(state='normal'), self.logbox.insert('end', s+'\n'), self.logbox.see('end'), self.logbox.configure(state='disabled')))
    def progress(self, n): self.after(0, lambda: self.bar.configure(value=n))
    def start_upload(self):
        cookie = self.cookie.get('1.0','end').strip(); path = self.file.get().strip(); parent = self.parent.get().strip() or '0'
        if not cookie or not path: return messagebox.showwarning('提示','请填写 Cookie 并选择文件')
        self.start.configure(state='disabled'); self.bar['value'] = 0
        def run():
            try:
                client = UCClient(cookie, self.log, self.progress)
                if self.mode.get() == 'folder': client.upload_folder(path, parent)
                else: client.upload(path, parent)
            except Exception as e: self.log('失败: ' + str(e)); self.after(0, lambda: messagebox.showerror('上传失败', str(e)))
            finally: self.after(0, lambda: self.start.configure(state='normal'))
        threading.Thread(target=run, daemon=True).start()

if __name__ == '__main__': App().mainloop()
