# Features - 这城有良田 自动任务脚本

## 方案一：Android ADB（模拟器用户）

基于 Android Debug Bridge (ADB) 实现，适用于 MuMu / 蓝叠 / 夜神 等安卓模拟器。

- **截图与图像识别** - 通过 `adb shell screencap` 获取截图，OpenCV 模板/特征匹配定位 UI 元素
- **点击操作** - `adb shell input tap x y` 模拟点击
- **滑动操作** - `adb shell input swipe x1 y1 x2 y2 duration` 模拟滑动
- **文本输入** - `adb shell input text` 输入文本
- **按键事件** - `adb shell input keyevent` 模拟返回/Home 等按键
- **应用管理** - 启动/关闭游戏 App
- **多分辨率适配** - 根据设备分辨率动态缩放点击坐标

### 可自动化功能

- **主线任务推进** - 自动检测并执行主线任务
- **政务处理** - 自动完成政务派遣
- **建筑升级** - 自动升级住宅、县府、农田等核心建筑
- **施粥活动** - 定时执行施粥操作
- **县学传业** - 自动完成县学任务
- **招贤任务** - 自动招募僚属
- **巡视任务** - 自动派遣僚属执行巡视
- **税收收取** - 自动收取产业税收
- **不良司缉盗** - 自动完成缉盗任务
- **以武会友** - 自动挑战竞技场
- **领取奖励** - 自动领取各类任务/活动奖励
- **兑换码兑换** - 自动输入并兑换礼包码
- **异常处理** - 网络断开/弹窗/更新提示等自动处理

---

## 方案二：Python 屏幕截取 + 模拟键鼠（微信小程序 PC 端）

基于 Python 实现，适用于在电脑上运行微信小程序的用户。

- **窗口截图** - 通过 `PIL` / `mss` / `pygetwindow` 截取微信小游戏窗口区域
- **图像识别** - `OpenCV` 模板匹配定位按钮/图标
- **鼠标点击** - `pyautogui` / `pynput` 模拟鼠标点击
- **鼠标拖拽** - 模拟拖拽滑动操作
- **键盘模拟** - 模拟键盘快捷键（如截图、粘贴等）
- **窗口管理** - 检测微信窗口位置和大小，支持窗口移动/置顶
- **前台检测** - 确保游戏窗口处于激活状态再执行操作

### 可自动化功能

与方案一相同，区别在于交互方式由 ADB 替换为屏幕图像识别 + 鼠标模拟。

- 主线任务推进
- 政务处理
- 建筑升级
- 施粥活动
- 县学传业
- 招贤任务
- 巡视任务
- 税收收取
- 不良司缉盗
- 以武会友
- 领取奖励
- 兑换码兑换
- 异常处理

---

## 通用模块（两种方案共享）

- **任务调度器** - 定时执行每日任务，支持自定义执行顺序和时间间隔
- **日志系统** - 记录操作日志和截图，方便排查问题
- **配置文件** - 可自定义任务开关、执行间隔、分辨率参数等
- **通知推送** - 任务完成/异常时推送通知（企业微信/Server 酱等）

---

## JSON 任务配置（一套 JSON 跑双后端）

任务以 JSON 文件定义，`tasks/*.json` + `templates/**/*.png` + `schemas/task_schema.json`。

### 顶层结构

采用 `dict` 形式：`tasks` 按 `key` 定义单个任务，`flows` 按 `key` 数组组合执行顺序。

```json
{
  "version": 1,
  "defaults": { "interval": 1.0, "timeout": 10, "threshold": 0.85, "after": 0.5 },
  "tasks": {
    "backHome": { "desc": "回主城", "steps": [] },
    "collectTax": { "desc": "收税", "steps": [] }
  },
  "flows": {
    "daily": ["backHome", "collectTax"],
    "quick": ["backHome"]
  }
}
```

### Step 通用字段

| 字段 | 说明 |
|---|---|
| `id` / `comment` | 日志定位用 |
| `action` | 见下表 action 全集 |
| `times` | 点击次数 / 重复次数 |
| `interval` | 每次重复间隔（秒） |
| `timeout` | 识图类等待超时（秒） |
| `on_fail` | `continue \| abort \| next_task`，失败策略 |
| `after` | 步骤执行后等待（秒） |

### Action 全集

| action | 参数示例 | 说明 |
|---|---|---|
| `tap` | `{"x": 500, "y": 1500, "coord": "rel1000"}` | 直接点击，推荐 `rel1000` 相对坐标，双后端按实际分辨率换算 |
| `image_tap` | `{"template": "templates/btn_home.png", "threshold": 0.85, "offset": [0, 0], "click": true}` | 截图 → OpenCV 匹配 → 点中点，模板图用相对路径 |
| `swipe` | `{"x1": 500, "y1": 1500, "x2": 500, "y2": 500, "duration": 500}` | 滑动，同 tap 用相对坐标 |
| `text` | `{"value": "兑换码123"}` | ADB `input text` / PC 端剪贴粘贴，后端分发 |
| `keyevent` | `{"key": "back"}` | 返回 / Home 等按键 |
| `sleep` | `{"seconds": 1.5}` | 硬等待 |
| `wait_image` | `{"template": "...", "timeout": 15}` | 等图像出现，用于转场 |
| `wait_gone` | `{"template": "...", "timeout": 10}` | 等图像消失 |
| `exists` | `{"template": "...", "then": [], "else": []}` | 识图分支，处理弹窗 |
| `call` | `{"task": "backHome"}` | 子任务调用（key 组合原子能力） |
| `loop` | `{"count": 5, "do": []}` | 循环执行 |
| `launch` / `kill` | `{"package": "..."}` | 应用启动 / 关闭 |

### 完整示例

```json
{
  "version": 1,
  "defaults": { "interval": 1.0, "timeout": 10, "threshold": 0.85 },
  "tasks": {
    "backHome": {
      "desc": "回主城",
      "steps": [
        { "comment": "点小地图Home", "action": "image_tap", "template": "templates/btn_home.png", "times": 1, "timeout": 5, "on_fail": "continue" },
        { "action": "wait_image", "template": "templates/main_city.png", "timeout": 10 },
        { "action": "sleep", "seconds": 0.8 }
      ]
    },
    "collectTax": {
      "desc": "收税",
      "steps": [
        { "action": "call", "task": "backHome" },
        { "action": "image_tap", "template": "templates/tax_icon.png", "times": 2, "interval": 1.2 },
        { "action": "exists", "template": "templates/confirm.png", "then": [{ "action": "image_tap", "template": "templates/confirm.png" }] }
      ]
    }
  },
  "flows": {
    "daily": ["backHome", "collectTax"]
  }
}
```

### 执行层映射

- `core/task_schema.py`：定义 + `load_tasks(path)` + 校验（key 存在性、`call` 循环引用、模板文件存在性）。
- `core/backends.py`：`Backend` 抽象（`screenshot/tap/swipe/input_text/key`），`AdbBackend` / `ScreenBackend` 实现相对坐标换算。
- `core/runner.py`：实现 `times / interval / timeout / on_fail / call / loop / exists` 语义，对接 `TaskManager`。
- `core/vision.py`：OpenCV 模板匹配（阈值 / 灰度 / 多尺度）。
- UI：任务 checkbox + flow 选择从 JSON 动态生成，加“重载 JSON”按钮。

---

## 坐标拾取器 + 模板截图工具（坐标/模板来源）

`tap x,y` 和 `image_tap template` 必须靠工具生成，不手写。拾取器为坐标拾取 + 模板截图二合一。

### 输出规范

- JSON 只存 `rel1000` 相对坐标（0-1000）+ `template` 相对路径，绝对像素只在拾取时显示。
- 拾取器一键复制 JSON 片段：

```json
{ "action": "tap", "x": 512, "y": 845, "coord": "rel1000" }
{ "action": "image_tap", "template": "templates/btn_home.png", "threshold": 0.85 }
```

### Screen 模式（微信小程序 PC 端，本机直接取）

- 全屏浮层 + 快捷键（`F2` 拾取坐标，`F3` 框选存模板）。
- 实时显示三套坐标：屏幕绝对坐标 / 窗口相对坐标 / `rel1000` 坐标。
- 框选后弹窗命名 → 存 `templates/xxx.png` → 一键复制对应 `image_tap` 片段。
- 技术：`mss / PIL + pynput + customtkinter overlay`，复用现有 `ui/`。

### ADB 模式（模拟器，远端投屏点选）

- `adb screencap` 定时刷新（0.5-1s）显示在工具窗口，鼠标在图上点选 → 自动换算回设备分辨率坐标。
- 同样支持框选 → 保存模板图。模板图必须来自 `screencap` 原图，不用 PC 截屏，否则匹配阈值必挂。
- 附带 `tap / swipe` 试点按钮，点完立刻 `adb shell input tap` 验证。

### 实现映射

- `tools/picker.py`：独立窗口（不污染主 `App`），模式切换 `adb | screen`，图片展示区 + 坐标栏 + JSON 预览栏。
- `core/coords.py`：`abs <-> window_rel <-> rel1000 <-> adb` 四种换算函数，双后端共用。
- 校验：`load_tasks` 时检查模板图存在 + 打印缺失清单。
- 落地顺序：先 `Screen 本机拾取` 验证闭环，再加 `ADB 投屏点选`。
