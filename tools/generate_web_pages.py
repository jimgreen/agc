from copy import deepcopy
from pathlib import Path

from bs4 import BeautifulSoup


PUBLIC = Path(__file__).resolve().parents[1] / "web" / "public"
source_path = PUBLIC.parent.parent / "tools" / "_source_index.html"
if not source_path.exists():
    source_path = PUBLIC / "index.html"
source = BeautifulSoup(source_path.read_text(encoding="utf-8"), "html.parser")
body = source.body
sidebar = deepcopy(body.select_one("aside.sidebar"))
header = deepcopy(body.select_one("header.topbar"))
sections = {item.get("id", "").replace("view-", ""): deepcopy(item) for item in body.select("section.view")}
footer = deepcopy(body.select_one("footer.footer"))
toast = deepcopy(body.select_one("#toast"))
modal = deepcopy(body.select_one("#curveModal"))

labels = {"overview": "系统总览", "monitor": "实时监视", "config": "参数配置", "logs": "日志查询", "history": "历史查询"}
routes = {name: f"/{name}.html" for name in labels}

for item in sidebar.select(".nav-item"):
    name = item.get("data-view", "overview")
    replacement = source.new_tag("a", attrs={"class": item.get("class", []), "data-view": name, "href": routes[name]})
    replacement.extend(deepcopy(child) for child in item.contents)
    item.replace_with(replacement)

for name, label in labels.items():
    page = BeautifulSoup(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/styles.css"><script defer src="/app.js"></script></head><body></body></html>',
        "html.parser",
    )
    page.body["data-page"] = name
    title = page.new_tag("title")
    title.string = f"AGC 自动发电控制 · {label}"
    page.head.append(title)
    shell = page.new_tag("div", attrs={"class": "app-shell"})
    page_sidebar = deepcopy(sidebar)
    for nav in page_sidebar.select(".nav-item"):
        nav_class = list(nav.get("class", []))
        if nav.get("data-view") == name:
            if "active" not in nav_class:
                nav_class.append("active")
        else:
            nav_class = [item for item in nav_class if item != "active"]
        nav["class"] = nav_class
    shell.append(page_sidebar)
    main = page.new_tag("main", attrs={"class": "main-content"})
    page_header = deepcopy(header)
    heading = page_header.select_one("#viewTitle")
    if heading:
        heading.string = label
    main.append(page_header)
    page_section = deepcopy(sections[name])
    section_classes = list(page_section.get("class", []))
    if "active" not in section_classes:
        section_classes.append("active")
    page_section["class"] = section_classes
    main.append(page_section)
    main.append(deepcopy(footer))
    shell.append(main)
    page.body.append(shell)
    if toast:
        page.body.append(deepcopy(toast))
    if name == "monitor" and modal:
        page.body.append(deepcopy(modal))
    (PUBLIC / f"{name}.html").write_text("<!doctype html>\n" + str(page.html), encoding="utf-8")

(PUBLIC / "index.html").write_text(
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="0;url=/overview.html"><title>AGC 自动发电控制 · Web HMI</title></head><body><p>正在进入 <a href="/overview.html">系统总览</a>…</p></body></html>',
    encoding="utf-8",
)
print("generated", ", ".join(f"{name}.html" for name in labels))
