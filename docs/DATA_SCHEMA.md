# 数据格式

所有示例均为合成数据，只用于说明字段，不代表任何真实用户。

## 睡眠记录

`workspace/data/sleep/sleep.json`

```json
{
  "records": {
    "2026-01-01": {
      "date": "2026-01-01",
      "bedtime": "23:30",
      "wake": "07:30",
      "duration_min": 480,
      "target_min": 480,
      "deep_min_est": 90,
      "rem_min_est": 80,
      "night_wake_count": 1,
      "source": "watch"
    }
  }
}
```

- `date` 是醒来日；
- `source`：`watch` > `watch_manual` / `screenshot` > `oral`；
- 动态字段（`target_min`、`debt_after_min`）由 `sleep_store.py` 重算。

## 手表归档

`workspace/data/health/watch/DATE.json`

```json
{
  "date": "2026-01-01",
  "sleep": [
    {
      "start": 1767223800,
      "end": 1767252600,
      "header_totals": {"deep_s": 5400, "light_s": 18600, "rem_s": 4800, "awake_s": 600},
      "stages": [
        {"ts": 1767223800, "duration_s": 1800, "stage": 2}
      ]
    }
  ],
  "heart_rate": [{"ts": 1767223800, "bpm": 60}],
  "resting_heart_rate": [{"ts": 1767223800, "bpm": 55}],
  "spo2": [{"ts": 1767223800, "percent": 95}],
  "stress": [{"ts": 1767223800, "value": 35}],
  "activity_today": []
}
```

- 阶段码：`1=deep, 2=light, 3=rem, 4=awake`；
- `activity` 是当日累计快照，不求和；
- 心率/血氧/压力为稀疏采样，按 `ts` 去重。

## 精力自评

`workspace/data/health/energy/DATE.json`

```json
{
  "date": "2026-01-01",
  "level": 4,
  "score": 80,
  "raw": "4",
  "recorded_at": "2026-01-01 20:30"
}
```

## 精力事件

`workspace/data/health/energy/events.jsonl`

```json
{"date":"2026-01-01","ts":1767272400,"time":"15:00","level":3,"score":60,"raw":"下午有点困","tags":[],"time_provided":true,"time_estimated":false,"recorded_at":"2026-01-01 15:01"}
```

- 事件只追加，不覆盖当天整体自评；
- 同日期/时间/原文自动判重；
- 事件不直接参与当天精力分，只作为训练标签或时段模型素材。

## 冷启动问卷

`workspace/data/health/energy/onboarding.json`

```json
{
  "weekday_bed": "23:30",
  "weekday_wake": "07:30",
  "weekend_bed": "00:30",
  "weekend_wake": "09:00",
  "natural_sleep_min": 480,
  "chronotype": 3,
  "post_lunch_sleepiness": 1,
  "nap_caffeine": "偶尔小睡或咖啡",
  "completed_at": "2026-01-01 10:00"
}
```

## 反应力测试

`workspace/data/health/reaction/DATE.json`

```json
{
  "date": "2026-01-01",
  "tests": [
    {
      "date": "2026-01-01",
      "ts": 1767272400,
      "time": "15:00",
      "block": "下午",
      "time_since_wake_min": 450,
      "source": "desktop",
      "version": 4,
      "space_trials": 8,
      "started_at": "2026-01-01 15:00:00",
      "ended_at": "2026-01-01 15:01:00",
      "duration_s": 60,
      "trials": 8,
      "valid": 8,
      "median_ms": 260,
      "mean_ms": 270,
      "min_ms": 220,
      "max_ms": 340,
      "lapses": 0,
      "false_starts": 0
    }
  ]
}
```

日报会聚合为 `lapses_total`、`false_starts_total`、`time_since_wake_min`；不包含 `click_trials` 死字段。

## 恢复分输出

`workspace/data/health/recovery/DATE.json`

```json
{
  "date": "2026-01-01",
  "score": 78,
  "confidence": "高",
  "weight_coverage": 1.0,
  "missing": [],
  "components": {
    "duration": {"score": 100},
    "continuity": {"score": 100},
    "deep": {"score": 75, "personal": true},
    "hr_dip": {"score": 75, "dip": 0.08, "personal": true},
    "stage_gradient": {"score": 70, "gradient": 2.0, "personal": true},
    "spo2": {"score": 100, "low": 95, "min": 94, "n": 20},
    "stress": {"score": 75, "avg": 40.0, "n": 20, "personal": true}
  }
}
```
