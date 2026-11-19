# -*- coding: utf-8 -*-
"""
设计令牌（Design Tokens）—— 全界面唯一的数值来源。

为什么要有这一层：
  之前间距、字号、圆角散落在各处，随手填 3px / 7px / 11px 这类数值，
  版面看着"差不多"却总是不整齐，改一处要翻遍全项目。
  现在所有尺寸只能从这里的刻度上取，天然对齐、改动只需一处。

刻度依据（来自 Fluent 2 与 8pt 网格规范）：
  间距  4px 基准 —— 4 / 8 / 12 / 16 / 24 / 32
  字号  H1 20 · H2 16 · 正文 13 · 次要 12 · 说明 11（不低于 11，保证可读）
  圆角  控件 6 · 卡片 10 · 药丸 999（Fluent radiusLarge=6, xLarge=8 的近似）
  对比度 正文对背景 ≥ 4.5:1（WCAG AA）

配色遵循"主色 + 语义色，不用第三种高饱和色抢注意力"：
  中性色承担 90% 的界面，强调色只留给当前选中项与主操作按钮，
  红/绿/黄只表达危险/成功/警告，不参与装饰。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 间距
# 只用这几个值。需要别的距离时，先问自己能不能归到最近的档位。
SP = {
    "xxs": 4,    # 图标与文字之间
    "xs": 8,     # 同一组内相邻控件
    "s": 12,     # 控件与所属标签
    "m": 16,     # 卡片内边距（标准）
    "l": 24,     # 卡片之间、分区之间
    "xl": 32,    # 大分区之间
}

# ---------------------------------------------------------------- 字号
# 层级差要足够明显（至少 2px），否则看起来像"随手改的"而非有意设计。
FS = {
    "h1": 20,    # 页面标题
    "h2": 16,    # 分区标题 / 卡片主标题
    "body": 13,  # 正文
    "sub": 12,   # 次要信息
    "cap": 11,   # 说明、徽章（下限）
}

# ---------------------------------------------------------------- 圆角
RD = {
    "ctl": 6,    # 按钮、输入框
    "card": 10,  # 卡片
    "pill": 999, # 药丸标签
}

# ---------------------------------------------------------------- 尺寸
SZ = {
    "btn_h": 34,       # 标准按钮高度
    "btn_h_lg": 40,    # 主操作按钮
    "btn_h_sm": 28,    # 次要/紧凑按钮
    "entry_h": 34,     # 输入框
    "icon": 40,        # 卡片图标容器
    "icon_sm": 32,
    "sidebar": 220,    # 侧栏宽度
}

# ---------------------------------------------------------------- 配色
# 深色：不用纯黑（#000 会眩光），用深灰打底；文字不用纯白，用近白灰。
DARK = {
    "bg":        "#1b1b1f",   # 应用底色
    "surface":   "#24242a",   # 卡片/面板
    "surface2":  "#2c2c34",   # 悬浮、输入、次级块
    "surface3":  "#35353f",   # hover
    "line":      "#3a3a44",   # 分隔线、边框（低对比，不抢戏）
    "line_hi":   "#4a4a56",
    "text":      "#ececf1",   # 主文字
    "text2":     "#b0b0bd",   # 次要文字
    "muted":     "#90909a",   # 说明文字（对卡片底 4.88:1，满足 WCAG AA）
    "accent":    "#5b8cff",   # 强调（选中、主按钮）
    "accent_h":  "#7aa2ff",
    "accent_fg": "#ffffff",   # 强调底上的文字
    "ok":        "#4ec97e",
    "warn":      "#e8b339",
    "danger":    "#e5534b",
    "sel_bg":    "#2b3550",   # 选中态底色：强调色向底色混合，不与 accent 抢注意力
}

LIGHT = {
    "bg":        "#f4f5f7",
    "surface":   "#ffffff",
    "surface2":  "#f0f1f4",
    "surface3":  "#e6e8ec",
    "line":      "#dcdfe5",
    "line_hi":   "#c6cad2",
    "text":      "#22232a",
    "text2":     "#565a66",
    "muted":     "#71757f",   # 对 surface 约 4.6:1
    "accent":    "#2f6bff",
    "accent_h":  "#1f57e0",
    "accent_fg": "#ffffff",
    "ok":        "#1a8a4a",
    "warn":      "#a5680a",
    "danger":    "#c8342b",
    "sel_bg":    "#e4ecff",
}

# 令牌名 -> 两套主题都必须齐全（缺一个就会出现"半黑半白"）
_REQUIRED = ["bg", "surface", "surface2", "surface3", "line", "line_hi",
             "text", "text2", "muted", "accent", "accent_h", "accent_fg",
             "ok", "warn", "danger", "sel_bg"]


def tokens(dark: bool) -> dict:
    d = DARK if dark else LIGHT
    # 防御：任何一套主题漏定义都会在取用时立刻暴露，而不是渲染成怪颜色
    missing = [k for k in _REQUIRED if k not in d]
    if missing:
        raise KeyError(f"配色令牌缺失: {missing}")
    return d


def blend(hex_a: str, hex_b: str, ratio: float) -> str:
    """两色混合，用于 hover / 选中态的过渡色（避免手调一堆近似色）。"""
    try:
        a = hex_a.lstrip("#")
        b = hex_b.lstrip("#")
        ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        r = int(ar + (br - ar) * ratio)
        g = int(ag + (bg - ag) * ratio)
        bl = int(ab + (bb - ab) * ratio)
        return "#%02x%02x%02x" % (r, g, bl)
    except Exception:
        return hex_b

# ---------------------------------------------------------------- 自适应换行
def auto_wrap_label(label, pad: int = 28):
    """让标签的 wraplength 跟随父容器实际宽度，而不是写死像素。

    为什么必须动态：「你这边显示正常、朋友设备显示不全」里最隐蔽的一类。
    把 wraplength 写成固定像素，在宽屏上刚好，但在 1366 宽的窗口里文字会一直画到
    容器外面被裁掉；反过来在 4K 上又过早换行、留一大片空白。
    高 DPI 缩放还会让同一像素值对应不同的逻辑宽度，所以固定值永远不可靠。

    绑定到父容器的 <Configure>，用 add="+" 不覆盖已有事件；
    父容器尚未布局时给一个保守初值，after_idle 再校准一次。
    """
    parent = getattr(label, "master", None)
    if parent is None:
        return label
    try:
        label.configure(wraplength=max(180, 520 - pad))
    except Exception:
        return label

    def upd(_e=None):
        try:
            w = int(parent.winfo_width()) - int(pad)
            if w > 120:
                label.configure(wraplength=w)
        except Exception:
            pass

    try:
        parent.bind("<Configure>", upd, add="+")
        label.after_idle(upd)
    except Exception:
        pass
    return label


def auto_wrap_tree(root, default_pad: int = 48):
    """把一个容器里所有「写死了 wraplength」的标签批量改成动态换行。

    页面里散落的固定像素换行值在窄窗口上会把文字画到容器外，
    而且在 150% DPI 缩放的设备上表现与开发机完全不同。统一收口到这里，
    页面构造末尾调一次即可，不必逐条改布局代码。
    """
    seen = set()

    def walk(w):
        try:
            if id(w) in seen:
                return
            seen.add(id(w))
            cur = int(w.cget("wraplength") or 0)
        except Exception:
            cur = 0
        if cur > 100:
            auto_wrap_label(w, pad=default_pad)
        try:
            for ch in w.winfo_children():
                walk(ch)
        except Exception:
            pass

    walk(root)
    return root
