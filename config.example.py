# -*- coding: utf-8 -*-
"""CMF Watch 同步配置文件模板"""
import os

# ADB 可执行文件路径
# Windows 示例: r'C:\platform-tools\adb.exe'
# macOS/Linux 示例: '/usr/local/bin/adb' 或 'adb' (如果在 PATH 中)
ADB_PATH = r'C:\platform-tools\adb.exe'

# 临时文件目录
TEMP_DIR = os.environ.get('TEMP', '/tmp')

# 导入脚本路径 (cmf_health_import.py 的位置)
IMPORT_SCRIPT = os.path.join(os.path.dirname(__file__), 'cmf_health_import.py')

# 7-Zip 可执行文件路径 (用于解压加密的 zip)
# Windows 示例: r'C:\Program Files\7-Zip\7z.exe'
# macOS/Linux 示例: '/usr/bin/7z' 或 '7z' (如果在 PATH 中)
SEVENZIP_PATH = r'C:\Program Files\7-Zip\7z.exe'

# CMF Watch App 包名
CMF_PACKAGE = 'com.nothing.smartcenter_cn'

# Chrome 浏览器 Activity
CHROME_ACTIVITY = 'com.android.chrome/com.google.android.apps.chrome.Main'

# 数据库路径 (cmf_health_import.py 会使用)
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'health.db')
RAW_DIR = os.path.join(os.path.dirname(__file__), 'data', 'raw')
