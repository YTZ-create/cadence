# -*- coding: utf-8 -*-
"""
CMF Watch 每周数据自动同步脚本

流程: adb连手机 -> 打开App导出页 -> 读密码 -> 复制链接 -> Chrome读链接 -> 电脑下载zip -> 解压 -> 入库

依赖:
- Android Debug Bridge (adb)
- 7-Zip (用于解压加密的 zip 文件)
- Python 3.7+

用法:
    python cmf_sync.py
"""
import subprocess
import sys
import re
import os
import time
import zipfile
import shutil

# 导入配置
try:
    import config
except ImportError:
    print("❌ 未找到 config.py，请复制 config.example.py 为 config.py 并修改配置")
    sys.exit(1)

sys.stdout.reconfigure(encoding='utf-8')

ADB = config.ADB_PATH
TMP = config.TEMP_DIR
EXPORT_DIR = os.path.join(TMP, 'cmf-export')
ZIP_PATH = os.path.join(TMP, 'cmf-export.zip')
IMPORT_SCRIPT = config.IMPORT_SCRIPT
SEVENZIP = config.SEVENZIP_PATH
PKG = config.CMF_PACKAGE
CHROME = config.CHROME_ACTIVITY


def sh(args, timeout=60):
    """执行 adb 命令"""
    r = subprocess.run([ADB] + args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    return r.stdout + r.stderr


def dump(tag):
    """uiautomator dump 到电脑，返回 xml 内容"""
    sh(['shell', 'rm', '-f', '/sdcard/uia_%s.xml' % tag])
    sh(['shell', 'uiautomator', 'dump', '/sdcard/uia_%s.xml' % tag])
    local = os.path.join(TMP, 'uia_%s.xml' % tag)
    sh(['pull', '/sdcard/uia_%s.xml' % tag, local])
    try:
        with open(local, encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print('dump 读取失败:', e)
        return ''


def nodes(xml):
    """解析出 (desc, text, bounds) 列表"""
    out = []
    for m in re.finditer(r'<node[^>]*/?>', xml):
        tag = m.group(0)
        d = re.search(r'content-desc="([^"]*)"', tag)
        t = re.search(r'text="([^"]*)"', tag)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        out.append({
            'desc': d.group(1) if d else '',
            'text': t.group(1) if t else '',
            'bounds': (int(b.group(1)), int(b.group(2)), int(b.group(3)), int(b.group(4))) if b else None,
        })
    return out


def center(b):
    if not b: return None
    return ((b[0]+b[2])//2, (b[1]+b[3])//2)


def find_and_click(xml, desc_kw=None, text_kw=None, desc_re=None, text_re=None):
    """在 UI 中查找元素并点击"""
    for n in nodes(xml):
        hit = False
        if desc_kw and desc_kw in n['desc']: hit = True
        if text_kw and text_kw in n['text']: hit = True
        if desc_re and re.search(desc_re, n['desc']): hit = True
        if text_re and re.search(text_re, n['text']): hit = True
        if hit and n['bounds']:
            c = center(n['bounds'])
            sh(['shell', 'input', 'tap', str(c[0]), str(c[1])])
            print('  点击:', n['desc'][:40] or n['text'][:40], '@', c)
            time.sleep(2)
            return True
    return False


def extract_text(xml, desc_kw=None, text_re=None, desc_re=None):
    """从 UI 中提取文本"""
    for n in nodes(xml):
        if text_re and re.search(text_re, n['text']): return n['text']
        if desc_kw and desc_kw in n['desc'] and n['text']: return n['text']
        if desc_re and re.search(desc_re, n['desc']) and n['text']: return n['text']
    return None


def ensure_device():
    """确保设备已连接"""
    out = sh(['devices'])
    if 'device\n' in out or '\tdevice' in out:
        print('✅ 设备已连接')
        return True
    # mdns 发现
    print('尝试 mdns 发现...')
    md = sh(['mdns', 'services'])
    print(md)
    m = re.search(r'(\d+\.\d+\.\d+\.\d+:\d+)', md)
    if m:
        sh(['connect', m.group(1)])
        time.sleep(3)
        out = sh(['devices'])
        if '\tdevice' in out:
            print('✅ 已通过 mdns 重连:', m.group(1))
            return True
    return False


def open_export_page():
    """确保 App 停在导出个人数据页"""
    sh(['shell', 'am', 'start', '-n', PKG + '/com.nothing.smartcenter.host.MainActivity'])
    time.sleep(5)
    xml = dump('exp')
    if '复制链接' in xml:
        print('✅ 已在导出页')
        return xml
    if find_and_click(xml, desc_kw='我的页面'):
        xml = dump('exp2')
        if find_and_click(xml, desc_kw='数据与隐私'):
            time.sleep(2)
            xml = dump('exp3')
            if '复制链接' in xml:
                print('✅ 已导航到导出页')
                return xml
    print('❌ 无法导航到导出页')
    return None


def read_password(xml):
    """从导出页提取 4-6 位数字密码"""
    for n in nodes(xml):
        t = (n['text'] or '').strip()
        d = (n['desc'] or '').strip()
        if re.fullmatch(r'\d{4,6}', t):
            return t
        if re.fullmatch(r'\d{4,6}', d):
            return d
    return None


def click_copy_link(xml):
    """点击复制链接按钮"""
    if find_and_click(xml, desc_kw='复制链接'):
        print('✅ 已点复制链接')
        return True
    return False


def read_link_from_chrome():
    """打开 Chrome，从剪贴板预览读出链接"""
    sh(['shell', 'am', 'start', '-n', CHROME])
    time.sleep(6)
    xml = dump('ch1')
    # 找地址栏/搜索框
    bar = None
    for n in nodes(xml):
        if n['bounds'] and ('url_bar' in n['desc'] or '搜索或输入网址' in n['desc'] or (n['text'] and '搜索或输入网址' in n['text'])):
            bar = n['bounds']; break
    if not bar:
        # 用 id 匹配
        m = re.search(r'<node[^>]*resource-id="com\.android\.chrome:id/(?:url_bar|search_box_text)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if m:
            bar = tuple(int(x) for x in m.groups())
    if not bar:
        print('❌ 找不到 Chrome 地址栏')
        return None
    c = center(bar)
    sh(['shell', 'input', 'tap', str(c[0]), str(c[1])])
    time.sleep(2)
    xml = dump('ch2')
    # 读剪贴板预览链接
    link = None
    for n in nodes(xml):
        t = n['text']
        if t and t.startswith('http'):
            link = t; break
    if not link:
        # 找 "您复制的链接" 行的 line_2
        m = re.search(r'<node[^>]*resource-id="com\.android\.chrome:id/line_2"[^>]*text="(https?[^"]+)"', xml)
        if m: link = m.group(1)
    if not link:
        # fallback: 长按地址栏 -> 粘贴 -> 读地址栏文本
        print('尝试长按粘贴...')
        sh(['shell', 'input', 'swipe', str(c[0]), str(c[1]), str(c[0]), str(c[1]), '1200'])
        time.sleep(2)
        xml = dump('ch3')
        if find_and_click(xml, text_kw='粘贴'):
            time.sleep(1)
            xml = dump('ch4')
            m = re.search(r'<node[^>]*resource-id="com\.android\.chrome:id/url_bar"[^>]*text="(https?[^"]*)"', xml)
            if m and m.group(1):
                link = m.group(1)
            if not link:
                for n in nodes(xml):
                    t = n['text']
                    if t and t.startswith('http'):
                        link = t; break
    if link:
        print('✅ 链接已读出:', link[:60], '...')
        return link
    print('❌ Chrome 中未找到链接（剪贴板可能为空）')
    return None


def download_zip(url):
    """下载 zip 文件"""
    r = subprocess.run(['curl.exe', '-sL', '-o', ZIP_PATH, '--max-time', '120', url],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if os.path.exists(ZIP_PATH) and os.path.getsize(ZIP_PATH) > 1000:
        print('✅ zip 下载成功: %d bytes' % os.path.getsize(ZIP_PATH))
        return True
    print('❌ zip 下载失败:', r.stdout[-300:] if r.stdout else '')
    return False


def unzip_and_import(password):
    """解压并导入数据库"""
    if os.path.exists(EXPORT_DIR):
        shutil.rmtree(EXPORT_DIR)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    r = subprocess.run([SEVENZIP, 'x', ZIP_PATH, '-p' + password, '-o' + EXPORT_DIR, '-y'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    if r.returncode != 0 or not os.listdir(EXPORT_DIR):
        print('❌ 解压失败:', r.stdout[-300:] if r.stdout else '')
        return False
    print('✅ 解压成功，文件数:', len(os.listdir(EXPORT_DIR)))
    r = subprocess.run([sys.executable, '-X', 'utf8', IMPORT_SCRIPT], capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=300)
    print(r.stdout)
    if r.returncode != 0:
        print('⚠️ 入库脚本输出(尾部):', r.stdout[-500:])
    return True


def main():
    print('===== CMF Watch 自动同步 =====')
    if not ensure_device():
        print('❌ 手机未连接。请确认：1)手机在同一 Wi-Fi 2)无线调试开启 3)adb 已配对')
        return 1
    xml = open_export_page()
    if not xml: return 1
    pwd = read_password(xml)
    print('🔑 密码:', pwd if pwd else '未读到')
    if not pwd:
        print('❌ 无法读取导出密码'); return 1
    if not click_copy_link(xml):
        print('⚠️ 无法点击复制链接（可能 7 天内不可重复导出），尝试直接读剪贴板...')
    link = read_link_from_chrome()
    if not link: return 1
    if not download_zip(link): return 1
    unzip_and_import(pwd)
    print('===== 同步完成 =====')
    return 0


if __name__ == '__main__':
    sys.exit(main())
