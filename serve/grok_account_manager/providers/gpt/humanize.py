"""拟人化交互：逐字符输入、鼠标移动轨迹、随机停顿。

自动化脚本最容易被识别的不是「值填得对不对」，而是「怎么填的」：

- 一次 ``value = 整串`` 再补一个 ``input`` 事件 —— 真实用户是逐字符敲的，
  页面会看到 ``keydown`` / ``beforeinput`` / ``input`` / ``keyup`` 四段事件；
- 直接 ``element.click()`` —— 真实用户点击前鼠标是从别处移动过来的，
  页面会看到一串 ``pointermove`` / ``mousemove``；
- 固定 sleep —— 人类行为是抖动的。

本模块提供三件事：鼠标轨迹、逐字符输入、随机停顿。全部是纯前端行为，
不改变页面最终状态，也不影响现有选择器逻辑。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

# 注入页面的辅助函数。重复注入是幂等的（window.__humanReady 守卫）。
HUMAN_HELPERS_JS = r"""
(function () {
    if (window.__humanReady) return true;

    function registerPointer(name) {
        return function () { return false; };
    }

    // 沿二次贝塞尔曲线把鼠标移到目标元素，并派发 pointermove / mousemove。
    window.__humanMoveTo = function (node) {
        if (!node) return false;
        var rect;
        try { rect = node.getBoundingClientRect(); } catch (error) { return false; }
        if (!rect || rect.width <= 0 || rect.height <= 0) return false;

        var tx = rect.left + rect.width * (0.3 + Math.random() * 0.4);
        var ty = rect.top + rect.height * (0.3 + Math.random() * 0.4);
        var sx = window.innerWidth * (0.15 + Math.random() * 0.7);
        var sy = window.innerHeight * (0.15 + Math.random() * 0.7);
        var cx = (sx + tx) / 2 + (Math.random() - 0.5) * 160;
        var cy = (sy + ty) / 2 + (Math.random() - 0.5) * 160;
        var count = 14 + Math.floor(Math.random() * 12);

        for (var i = 1; i <= count; i++) {
            var t = i / count;
            var inv = 1 - t;
            var x = inv * inv * sx + 2 * inv * t * cx + t * t * tx;
            var y = inv * inv * sy + 2 * inv * t * cy + t * t * ty;
            var options = {
                bubbles: true, cancelable: true, composed: true, view: window,
                clientX: x, clientY: y, screenX: x, screenY: y,
                button: 0, buttons: i === count ? 1 : 0,
            };
            try {
                node.dispatchEvent(new PointerEvent('pointermove', options));
            } catch (error) {}
            try {
                node.dispatchEvent(new MouseEvent('mousemove', options));
            } catch (error) {}
        }

        window.__humanPointer = { x: tx, y: ty };
        return true;
    };

    // 在鼠标当前位置按下并抬起。
    window.__humanClickAt = function (node) {
        if (!node) return false;
        var pointer = window.__humanPointer;
        var x = pointer ? pointer.x : 0;
        var y = pointer ? pointer.y : 0;
        if (!pointer) {
            var rect;
            try { rect = node.getBoundingClientRect(); } catch (error) { return false; }
            if (!rect) return false;
            x = rect.left + rect.width / 2;
            y = rect.top + rect.height / 2;
        }
        var down = {
            bubbles: true, cancelable: true, composed: true, view: window,
            clientX: x, clientY: y, screenX: x, screenY: y,
            button: 0, buttons: 1,
        };
        var up = {
            bubbles: true, cancelable: true, composed: true, view: window,
            clientX: x, clientY: y, screenX: x, screenY: y,
            button: 0, buttons: 0,
        };
        try { node.dispatchEvent(new PointerEvent('pointerdown', down)); } catch (error) {}
        try { node.dispatchEvent(new MouseEvent('mousedown', down)); } catch (error) {}
        try { node.focus({ preventScroll: true }); } catch (error) {}
        try { node.dispatchEvent(new PointerEvent('pointerup', up)); } catch (error) {}
        try { node.dispatchEvent(new MouseEvent('mouseup', up)); } catch (error) {}
        try { node.dispatchEvent(new MouseEvent('click', up)); } catch (error) {}
        return true;
    };

    window.__humanReady = true;
    return true;
})();
"""

# 单字符输入：事件顺序与浏览器原生一致 —— keydown → beforeinput → 值变化 → input → keyup。
#
# 目标元素在脚本内部自行定位，不作为参数传入：DOM 元素无法安全地跨 CDP 传参，
# 而且每次按键都重新定位一次也更接近真实浏览器的行为（焦点可能已经变化）。
_TYPE_CHAR_TEMPLATE = r"""
(function () {
    const target = __LOCATE__;
    const ch = __CHAR__;
    const code = __CODE__;
    const keyCode = __KEYCODE__;
    const shiftKey = __SHIFT__;
    if (!target) return 'missing';

    try { target.focus({ preventScroll: true }); } catch (error) {}

    const keyOptions = {
        key: ch, code: code, keyCode: keyCode, which: keyCode,
        location: 0, shiftKey: !!shiftKey, ctrlKey: false, altKey: false, metaKey: false,
        bubbles: true, cancelable: true, composed: true,
    };
    try { target.dispatchEvent(new KeyboardEvent('keydown', keyOptions)); } catch (error) {}

    try {
        target.dispatchEvent(new InputEvent('beforeinput', {
            bubbles: true, cancelable: true, composed: true,
            data: ch, inputType: 'insertText',
        }));
    } catch (error) {}

    try {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        )?.set;
        const current = String(target.value || '');
        if (setter) {
            setter.call(target, current + ch);
        } else {
            target.value = current + ch;
        }
    } catch (error) {
        return 'error';
    }

    try {
        target.dispatchEvent(new InputEvent('input', {
            bubbles: true, cancelable: true, composed: true,
            data: ch, inputType: 'insertText',
        }));
    } catch (error) {}

    try { target.dispatchEvent(new KeyboardEvent('keyup', keyOptions)); } catch (error) {}

    return 'ok';
})();
"""

# 清空输入框（模拟全选后删除）。
_CLEAR_TEMPLATE = r"""
(function () {
    const target = __LOCATE__;
    if (!target) return 'missing';
    try { target.focus({ preventScroll: true }); } catch (error) {}
    try {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        )?.set;
        if (setter) { setter.call(target, ''); } else { target.value = ''; }
    } catch (error) {
        return 'error';
    }
    try {
        target.dispatchEvent(new InputEvent('input', {
            bubbles: true, cancelable: true, composed: true,
            data: null, inputType: 'deleteContentBackward',
        }));
    } catch (error) {}
    return 'ok';
})()
"""

# 找到可见、可编辑的输入框；返回一个 JS 表达式（求值结果是元素或 null）。
def visible_input_js(selectors: str) -> str:
    """生成「定位可见输入框」的 JS 表达式。"""
    return (
        "(function () {"
        "function isVisible(node) {"
        "if (!node) return false;"
        "const style = window.getComputedStyle(node);"
        "if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;"
        "const rect = node.getBoundingClientRect();"
        "return rect.width > 0 && rect.height > 0;"
        "}"
        "const nodes = Array.from(document.querySelectorAll(%s));"
        "return nodes.find(node => isVisible(node) && !node.disabled && !node.readOnly) || null;"
        "})()" % json.dumps(selectors)
    )


def _key_info(char: str) -> tuple[str, int, bool]:
    """返回 (code, keyCode, shiftKey)。"""
    if "a" <= char <= "z":
        return f"Key{char.upper()}", ord(char.upper()), False
    if "A" <= char <= "Z":
        return f"Key{char}", ord(char), True
    if "0" <= char <= "9":
        return f"Digit{char}", ord(char), False
    specials = {
        " ": ("Space", 32, False),
        "@": ("Digit2", 50, True),
        ".": ("Period", 190, False),
        "-": ("Minus", 189, False),
        "_": ("Minus", 189, True),
        "+": ("Equal", 187, True),
        "!": ("Digit1", 49, True),
        "#": ("Digit3", 51, True),
        "$": ("Digit4", 52, True),
        "%": ("Digit5", 53, True),
        "&": ("Digit7", 55, True),
        "*": ("Digit8", 56, True),
        "(": ("Digit9", 57, True),
        ")": ("Digit0", 48, True),
    }
    return specials.get(char, ("Unidentified", 0, False))


def human_pause(low: float = 0.08, high: float = 0.32) -> float:
    """随机停顿并返回实际睡眠秒数。"""
    seconds = random.uniform(low, high)
    time.sleep(seconds)
    return seconds


def type_text(
    page: Any,
    value: str,
    *,
    locate_js: str,
    stop_event: Any = None,
    per_char: tuple[float, float] = (0.05, 0.18),
) -> bool:
    """按人类节奏逐字符输入。

    每次按键都会派发完整的键盘事件序列，并在字符之间插入随机间隔；
    空格位置会多停顿一点，接近真人打字节奏。
    """
    if not value:
        return True
    text = str(value)
    clear_script = _CLEAR_TEMPLATE.replace("__LOCATE__", locate_js)
    try:
        page.run_js(clear_script, as_expr=True)
    except Exception:
        # 清空失败不致命，后面逐字符写入同样能得到正确内容。
        pass

    for index, char in enumerate(text):
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            raise RuntimeError("任务已停止")
        code, key_code, shift_key = _key_info(char)
        script = (
            _TYPE_CHAR_TEMPLATE.replace("__LOCATE__", locate_js)
            .replace("__CHAR__", json.dumps(char))
            .replace("__CODE__", json.dumps(code))
            .replace("__KEYCODE__", str(key_code))
            .replace("__SHIFT__", "true" if shift_key else "false")
        )
        try:
            # as_expr=True：模板本身是 IIFE 表达式，必须按表达式求值，
            # 否则 DrissionPage 会把它包进 function(){...} 而丢掉返回值。
            result = page.run_js(script, as_expr=True)
        except Exception:
            return False
        if result == "missing":
            return False
        # 空格前后稍微停顿一下，模拟换词时的迟疑。
        if char == " ":
            delay = random.uniform(per_char[0], per_char[1]) + random.uniform(0.05, 0.18)
        else:
            delay = random.uniform(*per_char)
        if index < len(text) - 1:
            time.sleep(delay)
    return True


# 可复用的「鼠标轨迹 + 点击」函数体。
#
# 抽成独立常量，是为了让它既能被 click_element() 单独调用，也能被嵌进
# 「查找按钮 + 点击」的同一段脚本里——后者是并发下不丢点击的关键：
# 查找与点击之间不隔任何一次 run_js，页面跳转也不会让元素引用失效。
CLICK_TARGET_JS = r"""
function __humanClick(target) {
    if (!target) return 'missing';
    try { target.scrollIntoView({ block: 'center', inline: 'center' }); } catch (error) {}

    let rect = null;
    try { rect = target.getBoundingClientRect(); } catch (error) {}
    if (!rect || rect.width <= 0 || rect.height <= 0) return 'not-visible';

    // 鼠标从画面另一处沿二次贝塞尔曲线移过来，再在目标上按下抬起。
    const tx = rect.left + rect.width * (0.3 + Math.random() * 0.4);
    const ty = rect.top + rect.height * (0.3 + Math.random() * 0.4);
    const sx = window.innerWidth * (0.15 + Math.random() * 0.7);
    const sy = window.innerHeight * (0.15 + Math.random() * 0.7);
    const cx = (sx + tx) / 2 + (Math.random() - 0.5) * 160;
    const cy = (sy + ty) / 2 + (Math.random() - 0.5) * 160;
    const count = 14 + Math.floor(Math.random() * 12);

    for (let i = 1; i <= count; i++) {
        const t = i / count;
        const inv = 1 - t;
        const x = inv * inv * sx + 2 * inv * t * cx + t * t * tx;
        const y = inv * inv * sy + 2 * inv * t * cy + t * t * ty;
        const options = {
            bubbles: true, cancelable: true, composed: true, view: window,
            clientX: x, clientY: y, screenX: x, screenY: y,
            button: 0, buttons: i === count ? 1 : 0,
        };
        try { target.dispatchEvent(new PointerEvent('pointermove', options)); } catch (error) {}
        try { target.dispatchEvent(new MouseEvent('mousemove', options)); } catch (error) {}
    }

    const down = {
        bubbles: true, cancelable: true, composed: true, view: window,
        clientX: tx, clientY: ty, screenX: tx, screenY: ty, button: 0, buttons: 1,
    };
    const up = {
        bubbles: true, cancelable: true, composed: true, view: window,
        clientX: tx, clientY: ty, screenX: tx, screenY: ty, button: 0, buttons: 0,
    };
    try { target.dispatchEvent(new PointerEvent('pointerdown', down)); } catch (error) {}
    try { target.dispatchEvent(new MouseEvent('mousedown', down)); } catch (error) {}
    try { target.focus({ preventScroll: true }); } catch (error) {}
    try { target.dispatchEvent(new PointerEvent('pointerup', up)); } catch (error) {}
    try { target.dispatchEvent(new MouseEvent('mouseup', up)); } catch (error) {}
    try { target.dispatchEvent(new MouseEvent('click', up)); } catch (error) {}

    return 'clicked';
}
"""

# 完整的一次点击：定位 → 滚进视口 → 鼠标轨迹 → 按下抬起。
#
# ⚠️ 整段必须在**同一次** run_js 里完成。之前拆成「先标记 window.__xxxTarget、
# 下一次调用再点击」，结果页面一旦跳转，标记就指向已销毁的节点，点击永远发不出去
# ——表现就是「有时没有点到注册登录按钮」。
_CLICK_TEMPLATE = r"""
(function () {
    const target = __LOCATE__;
    if (!target) return 'missing';
    __HUMAN_CLICK__
})()
"""

# 极简兜底：只调用元素自带的 click()。
_PLAIN_CLICK_TEMPLATE = r"""
(function () {
    const target = __LOCATE__;
    if (!target) return 'missing';
    try { target.scrollIntoView({ block: 'center', inline: 'center' }); } catch (error) {}
    try {
        if (typeof target.click === 'function') { target.click(); return 'clicked'; }
    } catch (error) {}
    try { target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); return 'clicked'; } catch (error) {}
    return 'failed';
})()
"""

# 判断目标元素是否已经被前端框架接管。
#
# React 在 hydration 之前渲染出来的是纯静态 HTML：DOM 里有按钮，但没有任何事件
# 处理器，点击是空操作。并发时机器负载高、hydration 完成得晚，于是表现为
# 「按钮点了没反应 / 根本点不到」。React 接管后会在元素上挂 __reactProps$ /
# __reactFiber$ 之类的键，用它判断最直接。
_WIRED_TEMPLATE = r"""
(function () {
    const target = __LOCATE__;
    if (!target) return 'missing';
    try {
        for (const key in target) {
            if (key.indexOf('__reactProps$') === 0 || key.indexOf('__reactFiber$') === 0) {
                return 'wired';
            }
        }
    } catch (error) {}
    try { if (typeof target.onclick === 'function') return 'wired'; } catch (error) {}
    return 'static';
})()
"""


def _fill(script_template: str, locate_js: str) -> str:
    return (
        script_template.replace("__LOCATE__", locate_js)
        .replace("__HUMAN_CLICK__", CLICK_TARGET_JS)
    )


def element_state(page: Any, locate_js: str) -> str:
    """返回目标元素的状态：missing / wired / static。

    ``static`` 表示元素已经在 DOM 里、但前端框架还没接管它——此时点击不会生效。
    """
    try:
        return str(page.run_js(_fill(_WIRED_TEMPLATE, locate_js), as_expr=True) or "missing")
    except Exception:
        return "missing"


def click_element(page: Any, locate_js: str) -> str:
    """把鼠标移动到目标并点击。

    返回 ``clicked`` / ``missing`` / ``not-visible``。
    整段在一次 run_js 里完成，避免页面跳转后元素引用失效导致「点了没反应」。
    """
    try:
        status = str(page.run_js(_fill(_CLICK_TEMPLATE, locate_js), as_expr=True) or "")
    except Exception:
        status = ""
    if status == "clicked":
        return "clicked"
    # 兜底：轨迹派发失败时至少把 click() 打出去。
    try:
        fallback = str(page.run_js(_fill(_PLAIN_CLICK_TEMPLATE, locate_js), as_expr=True) or "")
    except Exception:
        fallback = ""
    return fallback or status or "failed"


__all__ = [
    "CLICK_TARGET_JS",
    "HUMAN_HELPERS_JS",
    "click_element",
    "element_state",
    "human_pause",
    "type_text",
    "visible_input_js",
]
