"""任务 JSON 构建器（开发者工具）。

坐标拾取器负责「取点/拍模板」，这个窗口负责「把点变成任务」：

- **点位池**：拾取器里连续点选的点位先堆在这里
- **步骤列表**：一段 JSON 数组，可直接手改；也可由按钮生成
- **校验**：用 :func:`core.task_schema.validate_task` 当场查错（未知 action、缺字段、模板缺失）
- **保存**：写进 ``tasks/*.json``，同名 key 覆盖、同文件其他任务与流程保留

最终产物就是一份别人可以直接用的任务配置：把 ``tasks/`` 目录发给对方，
主界面点「重载配置」即可看到新任务。

用法::

    python main.py builder                 # 从主程序或命令行打开
    python main.py builder --file tasks/rewards.json
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import task_schema  # noqa: E402

DEFAULT_FILE = "tasks_dev.json"

STEP_TEMPLATES = {
    "tap（固定坐标）": {"action": "tap", "x": 500, "y": 500, "coord": "rel1000"},
    "image_tap（识图点击）": {
        "action": "image_tap",
        "template": "templates/ui/btn_ok.png",
        "threshold": 0.85,
    },
    "tap_list（点位列表）": {
        "action": "tap_list",
        "id": "reward_rows",
        "default_selected": True,
        "items": [
            {"id": "r1", "name": "第 1 个", "x": 500, "y": 300},
            {"id": "r2", "name": "第 2 个", "x": 500, "y": 420},
        ],
    },
    "swipe（滑动）": {
        "action": "swipe",
        "x1": 500,
        "y1": 800,
        "x2": 500,
        "y2": 300,
        "coord": "rel1000",
        "duration": 400,
    },
    "sleep（等待）": {"action": "sleep", "seconds": 1.0},
    "wait_image（等出现）": {"action": "wait_image", "template": "templates/ui/btn_ok.png", "timeout": 8},
    "call（复用子任务）": {"action": "call", "task": "backHome"},
    "loop（循环）": {"action": "loop", "count": 3, "do": []},
}


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TaskBuilder(ctk.CTkToplevel):
    def __init__(self, master, root: Optional[str] = None, file: str = "", key: str = "", desc: str = ""):
        super().__init__(master)
        self.root_dir = os.path.abspath(root or project_root())
        self._points: List[dict] = []
        self._steps: List[dict] = []

        self.title("任务 JSON 构建器 · 开发者工具")
        self.geometry("1180x820")
        self.minsize(980, 700)

        self.key_var = ctk.StringVar(value=key or "myTask")
        self.desc_var = ctk.StringVar(value=desc or "我的任务")
        self.file_var = ctk.StringVar(value=file or DEFAULT_FILE)
        self.flow_var = ctk.StringVar(value="")

        self._build_ui()
        self.after(200, self._refresh_files)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 12}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=(12, 4), **pad)

        ctk.CTkLabel(head, text="任务 key").pack(side="left")
        ctk.CTkEntry(head, textvariable=self.key_var, width=150).pack(side="left", padx=(6, 14))
        ctk.CTkLabel(head, text="显示名").pack(side="left")
        ctk.CTkEntry(head, textvariable=self.desc_var, width=180).pack(side="left", padx=(6, 14))

        ctk.CTkLabel(head, text="写入文件").pack(side="left")
        self.file_box = ctk.CTkComboBox(
            head, variable=self.file_var, values=[DEFAULT_FILE], width=190
        )
        self.file_box.pack(side="left", padx=(6, 6))
        ctk.CTkButton(head, text="📥 载入已有任务", width=140, command=self.load_existing).pack(
            side="left", padx=6
        )

        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 8), **pad)
        ctk.CTkLabel(row2, text="加入流程（可空）").pack(side="left")
        ctk.CTkEntry(row2, textvariable=self.flow_var, width=120, placeholder_text="daily").pack(
            side="left", padx=(6, 14)
        )
        ctk.CTkLabel(
            row2,
            text="key 是任务标识（英数），显示名是界面上勾选框的文字；加入流程后每日流程会自动跑它。",
            text_color="gray",
        ).pack(side="left")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        # 左：步骤 JSON
        left = ctk.CTkFrame(body)
        left.pack(side="left", fill="both", expand=True)

        bar = ctk.CTkFrame(left, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 4), padx=8)
        ctk.CTkLabel(bar, text="步骤 steps（JSON 数组，可直接编辑）", font=ctk.CTkFont(size=13, weight="bold")).pack(
            side="left"
        )
        self.tpl_menu = ctk.CTkOptionMenu(
            bar,
            values=list(STEP_TEMPLATES),
            width=170,
            command=self.insert_template,
        )
        self.tpl_menu.set("＋ 插入步骤模板")
        self.tpl_menu.pack(side="right")
        ctk.CTkButton(bar, text="🗑 清空", width=70, command=self.clear_steps).pack(side="right", padx=6)
        ctk.CTkButton(bar, text="↶ 删除末步", width=90, command=self.pop_step).pack(side="right")

        self.steps_box = ctk.CTkTextbox(left, font=ctk.CTkFont(family="Menlo", size=12), wrap="none")
        self.steps_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.steps_box.insert("1.0", "[]")

        # 右：点位池
        right = ctk.CTkFrame(body, width=320)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        ctk.CTkLabel(
            right, text="点位池（拾取器连续采集）", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(anchor="w", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            right,
            text="在拾取器里点选后按「➕ 采集到任务」，\n点位会追加到这里。",
            text_color="gray",
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        self.points_frame = ctk.CTkScrollableFrame(right, height=260)
        self.points_frame.pack(fill="both", expand=True, padx=8)

        ctk.CTkButton(right, text="生成 tap_list（可勾选点位）", command=self.make_tap_list).pack(
            fill="x", padx=10, pady=(8, 3)
        )
        ctk.CTkButton(right, text="生成逐个 tap 步骤", command=self.make_tap_steps).pack(
            fill="x", padx=10, pady=3
        )
        ctk.CTkButton(
            right, text="清空点位池", fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self.clear_points,
        ).pack(fill="x", padx=10, pady=(3, 8))

        # 底部
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", pady=(0, 12), **pad)

        self.status = ctk.CTkLabel(foot, text="把步骤写好，然后校验 → 保存。", text_color="gray")
        self.status.pack(side="left")

        ctk.CTkButton(foot, text="💾 保存到 tasks/", width=150, command=self.save).pack(side="right")
        ctk.CTkButton(foot, text="📤 另存为…", width=110, command=self.save_as).pack(
            side="right", padx=6
        )
        ctk.CTkButton(foot, text="📋 复制 JSON", width=120, command=self.copy_json).pack(
            side="right", padx=6
        )
        ctk.CTkButton(foot, text="✔ 校验", width=90, command=self.validate).pack(side="right", padx=6)

    # ------------------------------------------------------------------ 点位池
    def add_point(self, rel_x: int, rel_y: int) -> None:
        self._points.append({"x": int(rel_x), "y": int(rel_y)})
        self._render_points()
        self._set_status(f"已采集第 {len(self._points)} 个点位 ({rel_x}, {rel_y})", "#5cb85c")

    def _render_points(self):
        for widget in self.points_frame.winfo_children():
            widget.destroy()
        if not self._points:
            ctk.CTkLabel(self.points_frame, text="（空）", text_color="gray").pack(anchor="w", pady=6)
            return
        for i, point in enumerate(self._points):
            row = ctk.CTkFrame(self.points_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            ctk.CTkLabel(row, text=f"p{i + 1}  ({point['x']}, {point['y']})", anchor="w").pack(
                side="left"
            )
            ctk.CTkButton(
                row, text="✕", width=28, fg_color="transparent",
                text_color=("#c0392b", "#ff6b6b"),
                command=lambda idx=i: self.remove_point(idx),
            ).pack(side="right")

    def remove_point(self, index: int) -> None:
        if 0 <= index < len(self._points):
            self._points.pop(index)
            self._render_points()

    def clear_points(self) -> None:
        self._points.clear()
        self._render_points()
        self._set_status("点位池已清空")

    # ------------------------------------------------------------------ 步骤
    def add_step(self, step: dict) -> None:
        try:
            steps = self._read_steps()
        except ValueError as exc:
            self._set_status(f"当前步骤 JSON 有误，先修好：{exc}", "#e74c3c")
            return
        steps.append(step)
        self._steps = steps
        self._write_steps(steps)
        self._set_status(f"已添加 {step.get('action')} 步骤（共 {len(steps)} 步）", "#5cb85c")

    def add_steps(self, steps: List[dict]) -> None:
        if not steps:
            return
        try:
            current = self._read_steps()
        except ValueError as exc:
            self._set_status(f"当前步骤 JSON 有误，先修好：{exc}", "#e74c3c")
            return
        current.extend(steps)
        self._steps = current
        self._write_steps(current)
        self._set_status(f"已添加 {len(steps)} 个步骤（共 {len(current)} 步）", "#5cb85c")

    def _read_steps(self) -> List[dict]:
        raw = self.steps_box.get("1.0", "end").strip() or "[]"
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}")
        if not isinstance(steps, list):
            raise ValueError("steps 必须是数组，例如 [ {\"action\": \"tap\", ...} ]")
        return steps

    def _write_steps(self, steps: List[dict]):
        self.steps_box.delete("1.0", "end")
        self.steps_box.insert("1.0", json.dumps(steps, ensure_ascii=False, indent=2))

    def pop_step(self):
        try:
            steps = self._read_steps()
        except ValueError:
            return
        if steps:
            steps.pop()
            self._write_steps(steps)
            self._set_status(f"已删除最后一步（剩 {len(steps)} 步）")

    def clear_steps(self):
        if messagebox.askyesno("确认", "清空所有步骤？", parent=self):
            self._write_steps([])
            self._set_status("已清空步骤")

    def insert_template(self, name: str):
        tpl = STEP_TEMPLATES.get(name)
        if tpl:
            self.add_step(json.loads(json.dumps(tpl)))
        self.tpl_menu.set("＋ 插入步骤模板")

    # ------------------------------------------------------------------ 合成
    def make_tap_list(self):
        if not self._points:
            self._set_status("点位池是空的，先在拾取器里采集点位", "#f0ad4e")
            return
        step = {
            "action": "tap_list",
            "id": f"{self.key_var.get().strip() or 'list'}_items",
            "comment": "点位列表：界面上按需勾选",
            "coord": "rel1000",
            "default_selected": True,
            "items": [
                {"id": f"p{i + 1}", "name": f"第 {i + 1} 个点位", "x": p["x"], "y": p["y"]}
                for i, p in enumerate(self._points)
            ],
        }
        self.add_step(step)

    def make_tap_steps(self):
        if not self._points:
            self._set_status("点位池是空的，先在拾取器里采集点位", "#f0ad4e")
            return
        steps = [
            {
                "action": "tap",
                "id": f"p{i + 1}",
                "comment": f"第 {i + 1} 个点位",
                "x": p["x"],
                "y": p["y"],
                "coord": "rel1000",
            }
            for i, p in enumerate(self._points)
        ]
        self.add_steps(steps)

    # ------------------------------------------------------------------ 校验
    def _current_body(self) -> Optional[dict]:
        key = self.key_var.get().strip()
        if not key:
            self._set_status("请先填任务 key", "#f0ad4e")
            return None
        try:
            steps = self._read_steps()
        except ValueError as exc:
            self._set_status(f"步骤 JSON 有误：{exc}", "#e74c3c")
            return None
        return {"desc": self.desc_var.get().strip() or key, "steps": steps}

    def validate(self) -> bool:
        body = self._current_body()
        if body is None:
            return False
        result = task_schema.validate_task(self.key_var.get().strip(), body, self.root_dir)
        if result.ok:
            self._set_status(result.summary(), "#5cb85c")
            if result.warnings or result.missing_templates:
                lines = list(result.warnings)
                if result.missing_templates:
                    lines.append("缺失模板：" + "、".join(result.missing_templates))
                messagebox.showinfo("校验通过，但有以下提醒", "\n".join(lines), parent=self)
            return True
        self._set_status(result.summary(), "#e74c3c")
        messagebox.showerror("校验未通过", "\n".join(result.errors), parent=self)
        return False

    # ------------------------------------------------------------------ 保存
    def _resolve_target(self) -> str:
        raw = self.file_var.get().strip() or DEFAULT_FILE
        if not raw.endswith(".json"):
            raw += ".json"
        if os.path.isabs(raw):
            return raw
        if os.sep in raw or "/" in raw:
            return os.path.normpath(os.path.join(self.root_dir, raw))
        return os.path.normpath(os.path.join(self.root_dir, "tasks", raw))

    def save(self) -> bool:
        if not self.validate():
            return False
        key = self.key_var.get().strip()
        body = self._current_body() or {}
        path = self._resolve_target()
        doc = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    doc = json.load(fp) or {}
            except (OSError, json.JSONDecodeError) as exc:
                if not messagebox.askyesno(
                    "文件读取失败", f"{path}\n{exc}\n\n仍然覆盖写入？", parent=self
                ):
                    return False
                doc = {}
        doc = task_schema.upsert_task_document(doc, key, body, self.flow_var.get().strip())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            task_schema.save_tasks_document(path, doc)
        except OSError as exc:
            self._set_status(f"写入失败：{exc}", "#e74c3c")
            return False
        others = [k for k in (doc.get("tasks") or {}) if k != key]
        self._set_status(
            f"已写入 {os.path.relpath(path, self.root_dir)}（任务 {key}"
            + (f"，同文件另有 {len(others)} 个任务" if others else "")
            + "）。回主界面点「重载配置」即可看到。",
            "#5cb85c",
        )
        self._refresh_files()
        return True

    def save_as(self):
        body = self._current_body()
        if body is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出任务 JSON",
            defaultextension=".json",
            initialdir=os.path.join(self.root_dir, "tasks"),
            initialfile=self.file_var.get().strip() or DEFAULT_FILE,
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        self.file_var.set(path)
        self.save()

    def copy_json(self):
        body = self._current_body()
        if body is None:
            return
        doc = task_schema.upsert_task_document(
            None, self.key_var.get().strip(), body, self.flow_var.get().strip()
        )
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status("任务 JSON 已复制到剪贴板", "#5cb85c")
        except Exception:
            self._set_status("复制失败，可手动选中步骤框复制", "#f0ad4e")

    # ------------------------------------------------------------------ 载入
    def _task_files(self) -> List[str]:
        folder = os.path.join(self.root_dir, "tasks")
        files = task_schema.task_files(folder)
        return [os.path.basename(f) for f in files]

    def _refresh_files(self):
        files = self._task_files()
        if files:
            self.file_box.configure(values=files)

    def load_existing(self):
        files = task_schema.task_files(os.path.join(self.root_dir, "tasks"))
        if not files:
            self._set_status("tasks/ 下还没有任务文件", "#f0ad4e")
            return
        options = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    doc = json.load(fp) or {}
            except (OSError, json.JSONDecodeError):
                continue
            for key, body in (doc.get("tasks") or {}).items():
                if isinstance(body, dict):
                    options.append(f"{os.path.basename(path)}::{key}")
        if not options:
            self._set_status("没有可载入的任务", "#f0ad4e")
            return

        dialog = _PickDialog(self, "载入已有任务", options)
        self.wait_window(dialog)
        choice = dialog.result
        if not choice:
            return
        filename, key = choice.split("::", 1)
        path = os.path.join(self.root_dir, "tasks", filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                doc = json.load(fp) or {}
        except (OSError, json.JSONDecodeError) as exc:
            self._set_status(f"读取失败：{exc}", "#e74c3c")
            return
        body = (doc.get("tasks") or {}).get(key) or {}
        self.key_var.set(key)
        self.desc_var.set(str(body.get("desc") or key))
        self.file_var.set(filename)
        self._points.clear()
        self._render_points()
        self._write_steps(body.get("steps") or [])
        self._set_status(f"已载入 {filename} 的 {key}（{len(body.get('steps') or [])} 步）", "#5cb85c")

    # ------------------------------------------------------------------ 工具
    def _set_status(self, text: str, color: str = "gray"):
        self.status.configure(text=text, text_color=color)


class _PickDialog(ctk.CTkToplevel):
    """简单的单选对话框（载入已有任务用）。"""

    def __init__(self, master, title: str, options: List[str]):
        super().__init__(master)
        self.title(title)
        self.geometry("460x420")
        self.result: Optional[str] = None
        self.transient(master)
        self.after(80, self.grab_set)

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(12, 6))
        self.box = ctk.CTkScrollableFrame(self)
        self.box.pack(fill="both", expand=True, padx=12, pady=6)
        for option in options:
            ctk.CTkButton(
                self.box,
                text=option,
                anchor="w",
                fg_color="transparent",
                border_width=1,
                text_color=("black", "white"),
                command=lambda o=option: self._choose(o),
            ).pack(fill="x", pady=2)
        ctk.CTkButton(self, text="取消", width=90, command=self.destroy).pack(pady=8)

    def _choose(self, option: str):
        self.result = option
        self.destroy()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="任务 JSON 构建器（开发者工具）")
    parser.add_argument("--file", default="", help="目标文件（相对项目根或 tasks/）")
    parser.add_argument("--key", default="", help="任务 key")
    parser.add_argument("--desc", default="", help="显示名")
    args = parser.parse_args(argv)

    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    root.title("任务 JSON 构建器")
    root.geometry("1180x820")
    builder = TaskBuilder(root, file=args.file, key=args.key, desc=args.desc)
    builder.pack(fill="both", expand=True)
    builder.after(300, builder.focus_force)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
