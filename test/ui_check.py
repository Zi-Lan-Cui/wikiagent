"""wiki-agent 前端自检：真实浏览器走一遍主路径，截图并抓 console 错误。

前提：Web 工作台已在跑（默认 http://127.0.0.1:8000）。
用法: .venv/bin/python test/ui_check.py [base_url]
输出: /tmp/ui/*.png；断言失败、脚本异常或 console error 时退出码 1。
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path("/tmp/ui")
OUT.mkdir(exist_ok=True)
failures: list[str] = []


def watch(page, tag):
    page.on("pageerror", lambda e: failures.append(f"[{tag}] pageerror: {e}"))
    page.on("console", lambda m: failures.append(f"[{tag}] console.error: {m.text}") if m.type == "error" else None)


def shot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  截图 {name}.png")


def check(cond, msg):
    if not cond:
        failures.append(msg)


with sync_playwright() as p:
    browser = p.chromium.launch()

    page = browser.new_page(viewport={"width": 1440, "height": 900})
    watch(page, "桌面")
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#session-list button", timeout=8000)
    shot(page, "home")

    page.click("#session-list button")
    page.wait_for_timeout(600)
    shot(page, "chat-session")

    # 草稿式新对话：以服务器会话数为准——点击绝不落库，反复点击回到同一页
    api_before = page.evaluate("fetch('/api/sessions').then(r => r.json()).then(x => x.length)")
    page.click("#new-session")
    page.click("#new-session")
    page.wait_for_timeout(300)
    api_after = page.evaluate("fetch('/api/sessions').then(r => r.json()).then(x => x.length)")
    check(api_after == api_before, f"点新对话在服务器创建了会话: {api_before} -> {api_after}")
    check(page.is_hidden(".chat-header"), "草稿页不该出现会话页头")
    check(page.is_visible("#draft-stage"), "草稿落地页未显示")
    check(page.evaluate("!document.getElementById('message-input').disabled"), "草稿页输入框不可用")
    check(
        page.evaluate("document.getElementById('message-form').parentElement.id") == "draft-stage",
        "composer 未移挂进草稿页",
    )
    zeros = page.evaluate(
        "[...document.querySelectorAll('.session-item small')].filter(x => x.textContent.startsWith('0')).length"
    )
    check(zeros == 0, f"侧栏仍显示空会话存根: {zeros}")
    shot(page, "new-chat-draft")
    page.click("#session-list button")
    page.wait_for_timeout(600)
    check(page.is_hidden("#draft-stage"), "选中会话后草稿页未收起")
    check(
        page.evaluate("document.getElementById('message-form').parentElement.className") == "chat-panel",
        "composer 未回挂到底部",
    )

    # 进入 Wiki 库：抽屉默认弹出，浮在占满全宽的阅读台上
    page.click("#wiki-open")
    page.wait_for_selector("#wiki-space:not([hidden])", timeout=8000)
    page.wait_for_selector("#wiki-files .wiki-file")
    drawer_cls = "document.getElementById('wiki-drawer').className"
    check("collapsed" not in page.evaluate(drawer_cls), "首次进入抽屉应默认展开")
    shot(page, "wiki-library")

    # 拖宽：按住抽屉右缘把手 +120px，宽度持久化
    handle = page.locator("#wiki-rail-resize")
    box = handle.bounding_box()
    page.mouse.move(box["x"] + 3, box["y"] + 300)
    page.mouse.down()
    page.mouse.move(box["x"] + 123, box["y"] + 300, steps=6)
    page.mouse.up()
    width1 = page.evaluate("document.getElementById('wiki-drawer').style.getPropertyValue('--rail-w')")
    check("420" in width1, f"拖宽未生效: {width1}")

    # 收起：抽屉平移出去，把手留在屏幕左缘，页面布局不变
    page.click("#wiki-rail-toggle")
    page.wait_for_timeout(300)
    check(page.evaluate(f"{drawer_cls}.includes('collapsed')"), "收起态未生效")
    shot(page, "wiki-rail-collapsed")

    # 刷新后：宽度记得；收起不跨访问——进库重新弹出
    page.reload(wait_until="networkidle")
    page.click("#wiki-open")
    page.wait_for_timeout(300)
    width2 = page.evaluate("document.getElementById('wiki-drawer').style.getPropertyValue('--rail-w')")
    check(width2 == width1, f"宽度未持久化: {width2}")
    check(not page.evaluate(f"{drawer_cls}.includes('collapsed')"), "重新进库抽屉应主动弹出")

    # 选中文件后抽屉应自动收拢，让位给阅读区（覆盖式抽屉的收合语义）
    page.click("#wiki-files .wiki-file")
    page.wait_for_selector("#wiki-content", state="visible", timeout=8000)
    page.wait_for_timeout(600)
    check(page.evaluate(f"{drawer_cls}.includes('collapsed')"), "选页后抽屉未自动收拢")
    shot(page, "wiki-view")
    page.locator(".wiki-page-header").screenshot(path=str(OUT / "wiki-header.png"))
    print("  截图 wiki-header.png")
    page.click("#wiki-close")

    page.click("#organize-open")
    page.wait_for_selector("#organize-view:not([hidden])", timeout=8000)
    page.wait_for_timeout(800)
    shot(page, "organize")

    page.click("#attention-open")
    page.wait_for_selector("#attention-view", state="visible", timeout=8000)
    page.wait_for_timeout(800)
    shot(page, "issues")
    page.close()

    mob = browser.new_page(viewport={"width": 390, "height": 780})
    watch(mob, "移动")
    mob.goto(BASE + "/", wait_until="networkidle")
    mob.wait_for_timeout(800)
    shot(mob, "mobile-home")
    for sel in ("#wiki-open", "#organize-open", "#attention-open"):
        if not mob.is_visible(sel):
            failures.append(f"[移动] 入口不可达: {sel}")
    mob.close()
    browser.close()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("OK：无脚本错误，断言全部通过")
