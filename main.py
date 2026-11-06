"""入口：GUI（默认）/ 命令行执行 / 配置自检 / 任务添加器。

    python main.py                          # 打开图形界面
    python main.py run --flow daily         # 命令行跑一遍每日流程
    python main.py run --mode mock          # 演练模式（不真实点击）
    python main.py check                    # 校验 tasks/*.json，列出缺失模板
    python main.py adder --mode adb         # 任务添加器（录任务、生成 JSON）
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def cmd_gui(_args) -> int:
    from ui.app import App

    App().mainloop()
    return 0


def cmd_check(args) -> int:
    from core.errors import TaskSchemaError
    from core.task_schema import load_tasks

    path = args.json or os.path.join(ROOT, "tasks")
    try:
        taskset = load_tasks(path, base_dir=ROOT)
    except TaskSchemaError as exc:
        print(f"✘ {exc}")
        return 1

    print(f"✔ 配置有效：{taskset.source}")
    print(f"  任务 {len(taskset.tasks)} 个 / 流程 {len(taskset.flows)} 个 / 步骤 {taskset.step_count()} 个")
    for name, keys in taskset.flows.items():
        mark = "  " if all(k in taskset.tasks for k in keys) else "✘ "
        print(f"{mark}flow {name}: {' → '.join(keys)}")
    for warning in taskset.warnings:
        print(f"  ! {warning}")
    if taskset.missing_templates:
        print(f"\n缺失模板 {len(taskset.missing_templates)} 张：")
        for rel in taskset.missing_templates:
            print(f"  - {rel}")
        print("用「任务添加器」框选保存到 templates/ 对应目录即可。")
    return 0


def _parse_vars(items) -> dict:
    result = {}
    for raw in items or []:
        if "=" not in raw:
            print(f"忽略无法解析的变量：{raw!r}（应为 key=value）")
            continue
        key, value = raw.split("=", 1)
        result[key.strip()] = value
    return result


def _parse_selection(items) -> dict:
    """``--select claimRewards::reward_rows=r1,r3`` → {"claimRewards::reward_rows": ["r1", "r3"]}

    键写 ``任务key::步骤id`` 最稳（不会和别的任务的同名步骤串台），
    只写 ``步骤id`` 也认，用于兼容老配置。
    """
    result = {}
    for raw in items or []:
        if "=" not in raw:
            print(f"忽略无法解析的点位选择：{raw!r}（应为 任务key::步骤id=点位id1,点位id2）")
            continue
        key, value = raw.split("=", 1)
        result[key.strip()] = [v.strip() for v in value.split(",") if v.strip()]
    return result


def cmd_run(args) -> int:
    from core.backends import create_backend
    from core.errors import TaskSchemaError
    from core.logger import Logger
    from core.notifier import Notifier
    from core.runner import Runner
    from core.task_schema import load_tasks
    from core.vision import TemplateMatcher

    logger = Logger(log_dir=os.path.join(ROOT, "logs"), level=args.log_level)
    path = args.json or os.path.join(ROOT, "tasks")
    try:
        taskset = load_tasks(path, base_dir=ROOT, logger=logger)
    except TaskSchemaError as exc:
        logger.error(str(exc))
        return 1

    mode = "mock" if args.dry_run else args.mode
    backend_cfg = {
        "adb_path": args.adb,
        "device": args.device,
        "resolution": args.resolution,
        "window_title": args.window,
        "capture_mode": args.capture,
        "region": [int(v) for v in args.region.split(",")] if args.region else None,
    }
    backend = create_backend(mode, backend_cfg, logger, templates_dir=os.path.join(ROOT, "templates"))

    selected = None
    if args.tasks:
        selected = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = [t for t in selected if t not in taskset.tasks]
        if unknown:
            logger.error(f"未知任务：{', '.join(unknown)}")
            return 1

    flow_keys = []
    for flow in args.flow:
        try:
            flow_keys.extend(taskset.resolve_flow(flow, enabled=selected))
        except KeyError as exc:
            logger.error(str(exc))
            return 1
    if not flow_keys:
        logger.error("没有可执行的任务")
        return 1

    stop_event = threading.Event()
    report = None
    try:
        backend.connect()
        logger.info(f"后端就绪：{backend.describe()}")
        runner = Runner(
            backend=backend,
            taskset=taskset,
            logger=logger,
            matcher=TemplateMatcher(threshold=float(taskset.defaults.get("threshold", 0.85))),
            stop_event=stop_event,
            variables=_parse_vars(args.var),
            selection=_parse_selection(args.select),
        )
        for _ in range(max(1, args.repeat)):
            report = runner.run(flow_keys)
            if not report.ok or stop_event.is_set():
                break
    except KeyboardInterrupt:
        stop_event.set()
        logger.warn("收到 Ctrl+C，已停止")
    except Exception as exc:
        logger.error(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            backend.close()
        except Exception:
            pass

    if report is not None and args.notify_type != "none":
        Notifier(
            {"type": args.notify_type, "url": args.notify_url, "sendkey": args.notify_url},
            logger,
        ).send("这城有良田 · 命令行执行结果", report.summary)

    if report is None:
        return 1
    print(f"\n{report.summary}\n日志：{logger.path}")
    return 0 if report.ok else 2


def cmd_adder(args) -> int:
    from tools.adder import main as adder_main

    argv = ["--mode", args.mode, "--device", args.device]
    for flag, value in (
        ("--adb", args.adb),
        ("--window", args.window),
        ("--file", getattr(args, "file", "")),
        ("--key", getattr(args, "key", "")),
        ("--desc", getattr(args, "desc", "")),
    ):
        if value:
            argv.extend([flag, value])
    return adder_main(argv)


def cmd_picker(args) -> int:
    """兼容别名：老命令行 ``picker`` 打开的就是任务添加器。"""
    print("提示：「任务添加器」已并入任务添加器，打开的是同一个窗口。")
    return cmd_adder(args)


def cmd_builder(args) -> int:
    """兼容别名：老命令行 ``builder`` 打开的就是任务添加器。"""
    print("提示：「任务构建器」已并入任务添加器，打开的是同一个窗口。")
    args.mode = "screen"
    args.device = "127.0.0.1:7555"
    args.adb = ""
    args.window = ""
    return cmd_adder(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="这城有良田 · 自动任务脚本")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="打开图形界面（默认）")

    check = sub.add_parser("check", help="校验任务 JSON")
    check.add_argument("--json", default="", help="任务文件或目录，默认 tasks/")

    run = sub.add_parser("run", help="命令行执行流程")
    run.add_argument("--flow", action="append", default=None, help="流程名，可重复；默认 daily")
    run.add_argument("--tasks", default="", help="只跑这些任务 key，逗号分隔")
    run.add_argument("--json", default="", help="任务文件或目录，默认 tasks/")
    run.add_argument("--mode", choices=["adb", "screen"], default="adb")
    run.add_argument("--dry-run", action="store_true", help="演练模式，不真实点击")
    run.add_argument("--adb", default="", help="adb 路径")
    run.add_argument("--device", default="127.0.0.1:7555", help="设备地址")
    run.add_argument("--resolution", default="", help="设备分辨率 宽x高")
    run.add_argument("--window", default="这城有良田", help="窗口标题（screen 模式）")
    run.add_argument("--capture", choices=["mss", "pil"], default="mss")
    run.add_argument("--region", default="", help="屏幕区域 x,y,w,h")
    run.add_argument("--repeat", type=int, default=1, help="重复轮数")
    run.add_argument("--var", action="append", default=[], help="运行时变量 key=value，可重复")
    run.add_argument(
        "--select",
        action="append",
        default=[],
        help="点位勾选：任务key::步骤id=点位id1,点位id2（可重复），缺省按 default_selected",
    )
    run.add_argument("--notify-type", choices=["none", "wecom", "serverchan", "webhook"], default="none")
    run.add_argument("--notify-url", default="", help="Webhook 地址或 SendKey")
    run.add_argument("--log-level", choices=["debug", "info", "warn", "error"], default="info")

    adder = sub.add_parser("adder", help="任务添加器（开发者）：录任务、生成 tasks/*.json")
    adder.add_argument("--mode", choices=["screen", "adb"], default="screen")
    adder.add_argument("--adb", default="", help="adb 路径")
    adder.add_argument("--device", default="127.0.0.1:7555", help="设备地址")
    adder.add_argument("--window", default="", help="窗口标题（屏幕模式）")
    adder.add_argument("--file", default="", help="目标文件，默认 tasks/tasks_dev.json")
    adder.add_argument("--key", default="", help="任务 key")
    adder.add_argument("--desc", default="", help="任务显示名")

    picker = sub.add_parser("picker", help="任务添加器（兼容别名，等同 adder）")
    picker.add_argument("--mode", choices=["screen", "adb"], default="screen")
    picker.add_argument("--adb", default="")
    picker.add_argument("--device", default="127.0.0.1:7555")
    picker.add_argument("--window", default="")

    builder = sub.add_parser("builder", help="任务构建器（兼容别名，等同 adder）")
    builder.add_argument("--file", default="", help="目标文件，默认 tasks/tasks_dev.json")
    builder.add_argument("--key", default="", help="任务 key")
    builder.add_argument("--desc", default="", help="显示名")

    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_gui(None)

    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "gui": cmd_gui,
        "check": cmd_check,
        "run": cmd_run,
        "adder": cmd_adder,
        "picker": cmd_picker,
        "builder": cmd_builder,
    }
    if args.cmd not in handlers:
        parser.print_help()
        return 1
    if args.cmd == "run" and not args.flow:
        args.flow = ["daily"]
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
