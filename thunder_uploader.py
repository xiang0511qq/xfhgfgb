import hashlib, json, mimetypes, os, threading, tkinter as tk
from tkinter import filedialog, messagebox, ttk
import requests
import boto3
from botocore.config import Config

API = 'https://api-pan.xunlei.com/drive/v1'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0 Safari/537.36'

class ThunderClient:
    def __init__(self, token, captcha, log, progress):
        self.token = token.strip()
        self.captcha = captcha.strip()
        self.log, self.progress = log, progress
        self.headers = {
            'Authorization': self.token if self.token.lower().startswith('bearer ') else 'Bearer ' + self.token,
            'X-Captcha-Token': self.captcha,
            'X-Device-Id': 'codex-desktop',
            'X-Client-Id': 'Xp6vsxz_7IYVw2BB',
            'X-Client-Version': '8.31.0.9726',
            'User-Agent': UA,
            'Accept': 'application/json',
        }

    def api(self, method, path, body=None):
        r = requests.request(method, API + path, headers=self.headers, json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f'迅雷接口 HTTP {r.status_code}: {r.text[:500]}')
        data = r.json()
        if isinstance(data, dict) and data.get('error'):
            raise RuntimeError(str(data))
        return data

    def gcid(self, path, size):
        block = 0x40000
        while size / block > 0x200 and block < 0x200000:
            block <<= 1
        outer = hashlib.sha1()
        with open(path, 'rb') as f:
            while True:
                part = f.read(block)
                if not part: break
                outer.update(hashlib.sha1(part).digest())
        return outer.hexdigest().upper()

    def create_folder(self, parent_id, name, space=''):
        d = self.api('POST', '/files', {'kind':'drive#folder','parent_id':parent_id,'name':name,'space':space})
        fid = (d.get('file') or d.get('data') or d).get('id')
        if not fid: raise RuntimeError(f'创建文件夹失败: {d}')
        self.log('已创建文件夹: ' + name)
        return fid

    def upload_file(self, path, parent_id, space=''):
        size = os.path.getsize(path); name = os.path.basename(path)
        self.log(f'准备上传: {name} ({size/1024/1024:.2f} MB)')
        gcid = self.gcid(path, size)
        init = self.api('POST', '/files', {'kind':'drive#file','parent_id':parent_id,'name':name,'size':size,'hash':gcid,'upload_type':'UPLOAD_TYPE_RESUMABLE','space':space})
        info = init.get('resumable', {}).get('params', {})
        if not info: raise RuntimeError(f'初始化上传失败: {init}')
        endpoint = info.get('endpoint','').strip()
        if not endpoint.startswith('http'): endpoint = 'https://' + endpoint
        client = boto3.client('s3', region_name='xunlei', endpoint_url=endpoint,
            aws_access_key_id=info.get('access_key_id'), aws_secret_access_key=info.get('access_key_secret'),
            aws_session_token=info.get('security_token'), config=Config(signature_version='s3v4'))
        sent = [0]
        def cb(n):
            sent[0] += n; self.progress(min(100, sent[0] * 100 / size))
        with open(path, 'rb') as f:
            client.upload_fileobj(f, info['bucket'], info['key'], Callback=cb,
                ExtraArgs={'ContentType': mimetypes.guess_type(name)[0] or 'application/octet-stream'})
        self.log('上传完成: ' + name)
        return (init.get('file') or {}).get('id')

    def upload_folder(self, folder, parent_id):
        folder = os.path.abspath(folder); root_name = os.path.basename(folder.rstrip(os.sep)) or '上传文件夹'
        root_id = self.create_folder(parent_id, root_name)
        ids = {folder: root_id}; files = []
        for cur, dirs, names in os.walk(folder):
            dirs.sort(); names.sort()
            for d in dirs:
                child = os.path.join(cur, d); ids[child] = self.create_folder(ids[cur], d)
            files += [os.path.join(cur, n) for n in names]
        for i, path in enumerate(files, 1):
            self.log(f'[{i}/{len(files)}] {os.path.basename(path)}')
            self.upload_file(path, ids[os.path.dirname(path)])
        self.log('文件夹上传完成: ' + root_name)
        return root_id

    def share(self, file_id, title):
        r = requests.post(API + '/share', headers={**self.headers, 'Content-Type':'application/json'}, json={
            'file_ids':[file_id], 'share_to':'copy', 'expiration_days':0,
            'pass_code_option':'NO_PASS_CODE', 'params':{}, 'title':title
        }, timeout=60)
        if r.status_code >= 400: raise RuntimeError(f'创建分享失败 HTTP {r.status_code}: {r.text[:500]}')
        d = r.json(); url = d.get('share_url') or (d.get('data') or {}).get('share_url')
        if not url: raise RuntimeError(f'分享接口未返回链接: {d}')
        return url

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('迅雷网盘文件上传'); self.geometry('640x560'); self.resizable(False, False)
        self.path = tk.StringVar(); self.parent = tk.StringVar(); self.mode = tk.StringVar(value='file')
        ttk.Label(self, text='Bearer Token（只在本机使用）').pack(anchor='w', padx=14, pady=(12,3))
        self.token = tk.Text(self, height=3, width=82); self.token.pack(padx=14)
        ttk.Label(self, text='x-captcha-token（接口需要时填写）').pack(anchor='w', padx=14, pady=(8,3))
        self.captcha = tk.Text(self, height=2, width=82); self.captcha.pack(padx=14)
        row=ttk.Frame(self); row.pack(fill='x', padx=14, pady=8)
        ttk.Label(row,text='目标文件夹 ID').pack(side='left'); ttk.Entry(row,textvariable=self.parent,width=32).pack(side='left',padx=8)
        b=ttk.Frame(self); b.pack(anchor='w', padx=14); ttk.Button(b,text='选择文件',command=self.choose).pack(side='left'); ttk.Button(b,text='选择文件夹',command=self.choose_folder).pack(side='left',padx=8)
        ttk.Entry(self,textvariable=self.path,width=82).pack(padx=14,pady=6)
        self.bar=ttk.Progressbar(self,length=610,mode='determinate'); self.bar.pack(padx=14,pady=8)
        self.start=ttk.Button(self,text='开始上传',command=self.start_upload); self.start.pack(pady=4)
        self.logbox=tk.Text(self,height=15,width=82,state='disabled'); self.logbox.pack(padx=14,pady=8)
    def choose(self):
        p=filedialog.askopenfilename();
        if p:self.path.set(p);self.mode.set('file')
    def choose_folder(self):
        p=filedialog.askdirectory();
        if p:self.path.set(p);self.mode.set('folder')
    def log(self,s): self.after(0,lambda:(self.logbox.configure(state='normal'),self.logbox.insert('end',s+'\n'),self.logbox.see('end'),self.logbox.configure(state='disabled')))
    def progress(self,n): self.after(0,lambda:self.bar.configure(value=n))
    def start_upload(self):
        token=self.token.get('1.0','end').strip(); captcha=self.captcha.get('1.0','end').strip(); path=self.path.get().strip(); parent=self.parent.get().strip()
        if not token or not path:return messagebox.showwarning('提示','请填写 Bearer Token 并选择文件')
        if not parent: return messagebox.showwarning('提示','请填写迅雷目标文件夹 ID')
        self.start.configure(state='disabled'); self.bar['value']=0
        def run():
            try:
                c=ThunderClient(token,captcha,self.log,self.progress)
                fid=c.upload_folder(path,parent) if self.mode.get()=='folder' else c.upload_file(path,parent)
                if fid:
                    try:self.log('永久分享链接: '+c.share(fid,os.path.basename(path.rstrip(os.sep))))
                    except Exception as e:self.log('上传成功，但分享失败: '+str(e))
            except Exception as e:self.log('失败: '+str(e));self.after(0,lambda:messagebox.showerror('上传失败',str(e)))
            finally:self.after(0,lambda:self.start.configure(state='normal'))
        threading.Thread(target=run,daemon=True).start()

if __name__=='__main__': App().mainloop()
