# -*- coding: utf-8 -*-
"""生成 MCSync 图标 app.ico（草方块 + 云），仅构建 exe 前需要"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角背景（蓝紫渐变，纵向）
    for y in range(s):
        t = y / max(1, s - 1)
        r = int(37 + (99 - 37) * t)
        g = int(99 + (102 - 99) * t)
        b = int(235 + (241 - 235) * t)
        d.line([(0, y), (s, y)], fill=(r, g, b, 255))

    # 遮罩出圆角
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.putalpha(mask)

    # 云（白色）
    cx, cy = s * 0.5, s * 0.42
    r = s * 0.13
    for ox, oy, rr in ((-0.16, 0.02, 0.13), (0.16, 0.02, 0.13), (0, -0.08, 0.17)):
        d.ellipse([cx + ox * s - rr * s, cy + oy * s - rr * s,
                   cx + ox * s + rr * s, cy + oy * s + rr * s], fill=(255, 255, 255, 242))
    d.rounded_rectangle([cx - s * 0.29, cy + s * 0.02, cx + s * 0.29, cy + s * 0.19],
                        radius=int(s * 0.08), fill=(255, 255, 255, 242))

    # 草方块（等距立方体，简化为正面 + 顶面）
    bw = s * 0.40
    bx = s * 0.5 - bw / 2
    by = s * 0.60
    bh = bw * 0.72
    # 正面：草绿
    d.rectangle([bx, by, bx + bw, by + bh], fill=(110, 168, 74, 255))
    # 顶面：亮草绿（平行四边形近似，用梯形）
    top_h = bw * 0.26
    d.polygon([(bx, by), (bx + bw, by), (bx + bw - bw * 0.16, by - top_h), (bx + bw * 0.16, by - top_h)],
              fill=(139, 195, 96, 255))
    # 侧面：泥土
    d.polygon([(bx + bw, by), (bx + bw - bw * 0.16, by - top_h),
               (bx + bw - bw * 0.16, by - top_h + bh), (bx + bw, by + bh)], fill=(134, 96, 67, 255))
    # 纹理点
    step = max(1, int(bw / 9))
    for i in range(int(bw / step)):
        for j in range(int(bh / step)):
            if (i + j) % 3 == 0:
                px = bx + i * step + step // 2
                py = by + j * step + step // 2
                d.point((px, py), fill=(94, 148, 62, 255))
    # 描边
    d.rectangle([bx, by, bx + bw, by + bh], outline=(60, 90, 40, 200), width=max(1, s // 64))
    return img


def main(out: str):
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = [draw(s) for s in sizes]
    imgs[0].save(out, format="ICO", sizes=[(s, s) for s in sizes],
                 append_images=imgs[1:])
    print("已生成图标:", out)


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main(os.path.join(here, "client", "app.ico"))
