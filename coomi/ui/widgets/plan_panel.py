"""PlanPanel — 即时渲染式多问题问询面板

单个 Widget + render() 方法，基于状态变量全量渲染 Rich 内容。
每次按键 → 更新状态 → refresh(layout=True) → 全量重新渲染。
无子 Widget 树，无 mount/unmount，无异步生命周期，无竞态。
"""
from __future__ import annotations

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.widget import Widget


class PlanPanel(Widget):
    """即时渲染式多问题问询面板"""

    def __init__(self, questions: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._questions = questions
        self._active_q = 0
        self._selected: dict[int, int] = {}
        self._multi_selected: dict[int, set[int]] = {}
        self._other_texts: dict[int, str] = {}
        for i in range(len(questions)):
            self._selected[i] = 0
            self._multi_selected[i] = set()
            self._other_texts[i] = ""

    @property
    def is_last_question(self) -> bool:
        return self._active_q == len(self._questions) - 1

    @property
    def _current_options_count(self) -> int:
        return len(self._questions[self._active_q].get("options", [])) + 1

    @property
    def _is_other_selected(self) -> bool:
        return self._selected.get(self._active_q, 0) == self._current_options_count - 1

    @property
    def _is_current_multi_select(self) -> bool:
        return bool(self._questions[self._active_q].get("multiSelect"))

    def render(self) -> Table:
        table = Table.grid(padding=(0, 1))

        # ── 导航栏 ──
        nav_parts = []
        for i, q in enumerate(self._questions):
            header = escape(str(q.get("header", f"Q{i+1}")))
            if i == self._active_q:
                nav_parts.append(f"[bold reverse] {header} [/bold reverse]")
            else:
                nav_parts.append(f"[dim]{header}[/dim]")
        table.add_row(Text.from_markup(" │ ".join(nav_parts)))
        table.add_row()

        # ── 问题 ──
        q = self._questions[self._active_q]
        table.add_row(Text.from_markup(
            f"[bold]Q{self._active_q + 1}: {escape(str(q['question']))}[/bold]"
        ))
        if q.get("recommendation"):
            table.add_row(Text.from_markup(
                f"  [dim]推荐: {escape(str(q['recommendation']))}[/dim]"
            ))

        # ── 选项 ──
        options = list(q.get("options", []))
        options.append({"label": "Other", "value": "__other__", "is_other": True})
        focus_idx = self._selected.get(self._active_q, 0)
        multi_select = bool(q.get("multiSelect"))

        for i, opt in enumerate(options):
            is_sel = (i == focus_idx)
            if multi_select and not opt.get("is_other"):
                checked = i in self._multi_selected.get(self._active_q, set())
                checkbox = "[x]" if checked else "[ ]"
                marker = f"[bold cyan]{checkbox}[/bold cyan]" if is_sel else checkbox
            else:
                marker = "[bold cyan]●[/bold cyan]" if is_sel else "○"
            if opt.get("is_other"):
                other_text = self._other_texts.get(self._active_q, "")
                if is_sel:
                    display = escape(other_text) if other_text else "[dim]输入自定义内容...[/dim]"
                    table.add_row(Text.from_markup(
                        f"  {marker} Other: {display}"
                    ))
                else:
                    display = escape(other_text) if other_text else "..."
                    table.add_row(Text.from_markup(
                        f"  {marker} Other: [dim]{display}[/dim]"
                    ))
            else:
                option_label = escape(str(opt["label"]))
                label = f"[bold]{option_label}[/bold]" if is_sel else option_label
                summary = _option_summary(opt)
                if summary:
                    label = f"{label} [dim]- {escape(summary)}[/dim]"
                rec = "  [dim]<- recommended[/dim]" if opt.get("is_recommended") else ""
                table.add_row(Text.from_markup(f"  {marker} {label}{rec}"))
                description = _option_description(opt)
                if description:
                    table.add_row(Text.from_markup(f"      [dim]{escape(str(description))}[/dim]"))

        # ── 操作提示 ──
        table.add_row()
        if self._is_current_multi_select:
            hint = "[↑↓ 选择  Space 勾选  ←→ 切换问题  Enter 确认  Esc 取消]"
        elif self.is_last_question:
            hint = "[←→ 切换问题  ↑↓ 选选项  Enter 确认  Esc 取消]"
        else:
            hint = "[←→ 切换问题  ↑↓ 选选项  Enter 下一问题  Esc 取消]"
        table.add_row(Text.from_markup(f"  [dim]{hint}[/dim]"))

        return table

    # ── 状态操作 ──

    def move_up(self) -> None:
        total = self._current_options_count
        idx = self._selected.get(self._active_q, 0)
        self._selected[self._active_q] = (idx - 1) % total
        self.refresh(layout=True)

    def move_down(self) -> None:
        total = self._current_options_count
        idx = self._selected.get(self._active_q, 0)
        self._selected[self._active_q] = (idx + 1) % total
        self.refresh(layout=True)

    def next_question(self) -> None:
        self._active_q = (self._active_q + 1) % len(self._questions)
        self.refresh(layout=True)

    def prev_question(self) -> None:
        self._active_q = (self._active_q - 1) % len(self._questions)
        self.refresh(layout=True)

    def set_other_text(self, text: str) -> None:
        """设置当前问题的 Other 文本并刷新显示"""
        self._other_texts[self._active_q] = text
        self.refresh()

    def toggle_current_option(self) -> None:
        """Toggle the focused option for a multi-select question."""
        if not self._is_current_multi_select or self._is_other_selected:
            return
        idx = self._selected.get(self._active_q, 0)
        selected = self._multi_selected.setdefault(self._active_q, set())
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)
        self.refresh(layout=True)

    def get_all_answers(self) -> dict[int, dict]:
        """收集所有问题的答案"""
        answers = {}
        for i, q in enumerate(self._questions):
            options = list(q.get("options", []))
            options.append({"label": "Other", "value": "__other__", "is_other": True})
            idx = self._selected.get(i, 0)
            if q.get("multiSelect"):
                selected_indices = sorted(self._multi_selected.get(i, set()))
                selected_options = [
                    options[opt_idx]
                    for opt_idx in selected_indices
                    if 0 <= opt_idx < len(options) and not options[opt_idx].get("is_other")
                ]
                labels = [opt["label"] for opt in selected_options]
                values = [opt.get("value", opt["label"]) for opt in selected_options]
                answers[i] = {
                    "option": values,
                    "label": ", ".join(labels),
                    "labels": labels,
                    "other_text": self._other_texts.get(i, "") or None,
                }
                continue
            opt = options[idx] if idx < len(options) else options[0]
            other_text = self._other_texts.get(i, "") if opt.get("is_other") else None
            answers[i] = {
                "option": opt.get("value", opt["label"]),
                "label": opt["label"],
                "other_text": other_text or None,
            }
        return answers


def _option_summary(option: dict) -> str:
    summary = option.get("summary") or option.get("preview") or ""
    return " ".join(str(summary).split())


def _option_description(option: dict) -> str:
    description = option.get("description") or ""
    return " ".join(str(description).split())
