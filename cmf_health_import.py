# -*- coding: utf-8 -*-
"""
CMF Watch 导出数据入库脚本（增量安全：INSERT OR IGNORE + 唯一约束）

将导出的 Excel 文件导入到 SQLite 数据库中，支持增量导入（不会重复插入相同时间戳的数据）

依赖:
- openpyxl
- sqlite3 (Python 内置)
"""
import openpyxl
import sqlite3
import glob
import os
import sys
import re

# 导入配置
try:
    import config
except ImportError:
    print("❌ 未找到 config.py，请复制 config.example.py 为 config.py 并修改配置")
    sys.exit(1)

sys.stdout.reconfigure(encoding='utf-8')

XLSX_DIR = os.path.join(os.environ.get('TEMP', '/tmp'), 'cmf-export')
DB_PATH = config.DB_PATH
RAW_DIR = config.RAW_DIR


def parse_num(v):
    """解析数值，返回 int 或 float，无效值返回 None"""
    if v is None: return None
    s = str(v).strip()
    if s == '' or s == 'null': return None
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except: return None


def get_sheet(f, name):
    """获取 Excel 工作表"""
    wb = openpyxl.load_workbook(f, read_only=False)
    ws = wb[name] if name in wb.sheetnames else wb.active
    return wb, ws


def rows_of(ws):
    """迭代工作表行（跳过表头）"""
    it = ws.iter_rows(values_only=True)
    next(it, None)  # skip header
    for r in it:
        yield r


def main():
    # 确保数据目录存在
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS heart_rate_24h (ts TEXT PRIMARY KEY, bpm REAL);
    CREATE TABLE IF NOT EXISTS resting_heart_rate (ts TEXT PRIMARY KEY, bpm REAL);
    CREATE TABLE IF NOT EXISTS stress (ts TEXT PRIMARY KEY, value REAL);
    CREATE TABLE IF NOT EXISTS blood_oxygen (ts TEXT PRIMARY KEY, value REAL);
    CREATE TABLE IF NOT EXISTS activity (ts TEXT PRIMARY KEY, steps REAL, total_cal REAL, active_cal REAL, exercise_min REAL, stand_count REAL);
    CREATE TABLE IF NOT EXISTS exercise (start_ts TEXT PRIMARY KEY, type TEXT, duration_min REAL, distance_km REAL, total_cal REAL, active_cal REAL, steps REAL, vo2max TEXT);
    CREATE TABLE IF NOT EXISTS account (k TEXT PRIMARY KEY, v TEXT);
    ''')
    counts = {}

    def ingest(table, cols, rows, key_idx=0):
        """导入数据到指定表"""
        n = 0
        for r in rows:
            vals = list(r)
            if len(vals) < len(cols): vals += [None] * (len(cols) - len(vals))
            vals = vals[:len(cols)]
            key = vals[key_idx]
            if key is None or str(key).strip() == '': continue
            placeholders = ','.join('?' * len(cols))
            cur.execute(f'INSERT OR IGNORE INTO {table} ({",".join(cols)}) VALUES ({placeholders})', vals)
            n += cur.rowcount
        counts[table] = counts.get(table, 0) + n

    # 心率
    for f in glob.glob(os.path.join(XLSX_DIR, '*心率_*.xlsx')):
        wb, ws = get_sheet(f, '二十四小时心率')
        ingest('heart_rate_24h', ['ts', 'bpm'], rows_of(ws))
        wb.close()
        wb, ws = get_sheet(f, '静息心率')
        ingest('resting_heart_rate', ['ts', 'bpm'], rows_of(ws))
        wb.close()
    # 压力
    for f in glob.glob(os.path.join(XLSX_DIR, '*压力_*.xlsx')):
        wb, ws = get_sheet(f, '压力')
        ingest('stress', ['ts', 'value'], rows_of(ws))
        wb.close()
    # 血氧
    for f in glob.glob(os.path.join(XLSX_DIR, '*血氧_*.xlsx')):
        wb, ws = get_sheet(f, '血氧数据')
        ingest('blood_oxygen', ['ts', 'value'], rows_of(ws))
        wb.close()
    # 今日活动
    for f in glob.glob(os.path.join(XLSX_DIR, '*今日活动_*.xlsx')):
        wb, ws = get_sheet(f, '今日活动')
        ingest('activity', ['ts', 'steps', 'total_cal', 'active_cal', 'exercise_min', 'stand_count'], rows_of(ws))
        wb.close()
    # 运动
    for f in glob.glob(os.path.join(XLSX_DIR, '*运动_*.xlsx')):
        wb, ws = get_sheet(f, '运动数据')
        it = ws.iter_rows(values_only=True)
        next(it, None)
        for r in it:
            if r[1] is None or str(r[1]).strip() == '': continue
            cur.execute('INSERT OR IGNORE INTO exercise (start_ts,type,duration_min,distance_km,total_cal,active_cal,steps,vo2max) VALUES (?,?,?,?,?,?,?,?)',
                        (str(r[1]), r[2], parse_num(r[3]), parse_num(r[4]), parse_num(r[10]), parse_num(r[11]), parse_num(r[12]), r[13] if r[13] else None))
            if cur.rowcount: counts['exercise'] = counts.get('exercise', 0) + 1
        wb.close()
    # 账号
    account_files = glob.glob(os.path.join(XLSX_DIR, '*账号*'))
    if account_files:
        f = account_files[0]
        wb, ws = get_sheet(f, '账号数据')
        it = ws.iter_rows(values_only=True)
        next(it, None)
        for r in it:
            if r[0] is None: continue
            for k, v in zip(['nickname','gender','birthday','height_cm','weight_kg'], r):
                if v is not None:
                    cur.execute('INSERT OR REPLACE INTO account (k,v) VALUES (?,?)', (k, str(v)))
        wb.close()

    conn.commit()
    print('=== 入库完成，本次新增 ===')
    for t, n in counts.items():
        print(f'  {t}: +{n}')
    print('=== 数据库总量 ===')
    for t in ['heart_rate_24h','resting_heart_rate','stress','blood_oxygen','activity','exercise']:
        n = cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {t}: {n}')
    print('=== 账号信息 ===')
    for k, v in cur.execute('SELECT k,v FROM account').fetchall():
        print(f'  {k}: {v}')
    conn.close()


if __name__ == '__main__':
    main()
