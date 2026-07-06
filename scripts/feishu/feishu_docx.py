#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书云文档 Docx 连接工具：创建文档 + 追加内容（只写）。

凭证从同目录 .env 读取：
  FEISHU_APP_ID / FEISHU_APP_SECRET （必填）
  FEISHU_FOLDER_TOKEN               （可选，新建文档放进哪个文件夹）
  FEISHU_DOC_ID                     （可选，append 不传 --doc 时用它）

用法：
    python feishu_docx.py create --title "知识库 2026"
    python feishu_docx.py create --title "周报" --folder fldrxxxxx

    python feishu_docx.py append --doc DOCID --text "一段文字"
    python feishu_docx.py append --doc DOCID --heading "二级标题" --level 2
    python feishu_docx.py append --doc DOCID --bullet "要点一" --bullet "要点二"
    python feishu_docx.py append --doc DOCID --ordered "第一步" --ordered "第二步"
    python feishu_docx.py append --doc DOCID --quote "引用一段"
    python feishu_docx.py append --doc DOCID --divider
    python feishu_docx.py append --doc DOCID --file knowledge/获客系统.md
    python feishu_docx.py append --doc DOCID --stdin < note.md

首次使用：复制 .env.example 为 .env 并填入真实凭证。
"""
import argparse
import os
import re
import sys
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.docx import v1 as docx
from lark_oapi.api.drive import v1 as drive
from lark_oapi.api.wiki import v2 as wiki
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
FOLDER_TOKEN = os.getenv("FEISHU_FOLDER_TOKEN", "").strip() or None
DEFAULT_DOC_ID = os.getenv("FEISHU_DOC_ID", "").strip() or None

DOC_URL_BASE = "https://feishu.cn/docx/"

BT_TEXT = 2
BT_BULLET = 12
BT_ORDERED = 13
BT_CODE = 14
BT_QUOTE = 15
BT_DIVIDER = 22
LEVEL_TO_BT = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8}
BT_TO_FIELD = {
    BT_TEXT: "text", BT_BULLET: "bullet", BT_ORDERED: "ordered",
    BT_CODE: "code", BT_QUOTE: "quote",
    3: "heading1", 4: "heading2", 5: "heading3",
    6: "heading4", 7: "heading5", 8: "heading6",
}

BATCH_LIMIT = 50


def build_client():
    if not APP_ID or not APP_SECRET:
        sys.exit("缺少凭证：请在 scripts/feishu/.env 填 FEISHU_APP_ID / FEISHU_APP_SECRET")
    return (lark.Client.builder()
            .app_id(APP_ID).app_secret(APP_SECRET)
            .log_level(lark.LogLevel.WARNING)
            .build())


def text_block(block_type, content):
    text = docx.Text.builder().elements([
        docx.TextElement.builder().text_run(
            docx.TextRun.builder().content(content).build()
        ).build()
    ]).build()
    builder = docx.Block.builder().block_type(block_type)
    getattr(builder, BT_TO_FIELD[block_type])(text)
    return builder.build()


def divider_block():
    return docx.Block.builder().block_type(BT_DIVIDER).divider(docx.Divider()).build()


def check(resp, action):
    if not resp.success():
        msg = f"{action}失败：code={resp.code} msg={resp.msg}"
        log = getattr(resp, "log_id", None)
        if log:
            msg += f" log_id={log}"
        raise RuntimeError(msg)


def create_doc(title, folder_token=None):
    client = build_client()
    body = docx.CreateDocumentRequestBody.builder().title(title)
    if folder_token:
        body = body.folder_token(folder_token)
    req = docx.CreateDocumentRequest.builder().request_body(body.build()).build()
    resp = client.docx.v1.document.create(req)
    check(resp, "创建文档")
    doc = resp.data.document
    return doc.document_id, DOC_URL_BASE + doc.document_id


def append_blocks(doc_id, blocks):
    if not blocks:
        return 0
    client = build_client()
    sent = 0
    for i in range(0, len(blocks), BATCH_LIMIT):
        chunk = blocks[i:i + BATCH_LIMIT]
        body = docx.CreateDocumentBlockChildrenRequestBody.builder().children(chunk).build()
        req = (docx.CreateDocumentBlockChildrenRequest.builder()
               .document_id(doc_id).block_id(doc_id)
               .request_body(body).build())
        resp = client.docx.v1.document_block_children.create(req)
        check(resp, "追加内容")
        sent += len(chunk)
    return sent


def add_member(doc_id, member_type, member_id, perm="edit"):
    client = build_client()
    body = (drive.BaseMember.builder()
            .member_type(member_type).member_id(member_id)
            .perm(perm).perm_type("container").build())
    req = (drive.CreatePermissionMemberRequest.builder()
           .token(doc_id).type("docx").need_notification(False)
           .request_body(body).build())
    resp = client.drive.v1.permission_member.create(req)
    check(resp, "添加协作者")
    return resp.data


def set_link_share(doc_id, link_entity, external):
    client = build_client()
    body = (drive.PermissionPublicRequest.builder()
            .external_access(external).link_share_entity(link_entity).build())
    req = (drive.PatchPermissionPublicRequest.builder()
           .token(doc_id).type("docx").request_body(body).build())
    resp = client.drive.v1.permission_public.patch(req)
    check(resp, "设置链接分享")
    return resp.data


FIELD_OF_BT = {
    2: "text", 3: "heading1", 4: "heading2", 5: "heading3",
    6: "heading4", 7: "heading5", 8: "heading6",
    12: "bullet", 13: "ordered", 14: "code", 15: "quote",
    16: "equation", 17: "todo",
}


def get_wiki_node(token):
    client = build_client()
    req = wiki.GetNodeSpaceRequest.builder().token(token).obj_type("wiki").build()
    resp = client.wiki.v2.space.get_node(req)
    check(resp, "查询 wiki 节点")
    return resp.data.node


def list_wiki_children(space_id, parent_node_token):
    client = build_client()
    all_items = []
    page_token = None
    while True:
        b = (wiki.ListSpaceNodeRequest.builder()
             .space_id(space_id).parent_node_token(parent_node_token)
             .page_size(50))
        if page_token:
            b = b.page_token(page_token)
        resp = client.wiki.v2.space_node.list(b.build())
        check(resp, "列出 wiki 子节点")
        data = resp.data
        all_items.extend(data.items or [])
        if not data.has_more:
            break
        page_token = data.page_token
    return all_items


def list_doc_blocks(doc_id):
    client = build_client()
    all_items = []
    page_token = None
    while True:
        b = docx.ListDocumentBlockRequest.builder().document_id(doc_id).page_size(500)
        if page_token:
            b = b.page_token(page_token)
        resp = client.docx.v1.document_block.list(b.build())
        check(resp, "读取文档块")
        all_items.extend(resp.data.items or [])
        if not resp.data.has_more:
            break
        page_token = resp.data.page_token
    return all_items


def render_block(b):
    bt = b.block_type
    if bt == 22:
        return "---"
    field = FIELD_OF_BT.get(bt)
    if not field:
        return None
    t = getattr(b, field, None)
    if not t or not t.elements:
        return None
    content = "".join(e.text_run.content for e in t.elements if e and e.text_run)
    if bt == 2:
        return content
    if 3 <= bt <= 8:
        return "#" * (bt - 2) + " " + content
    if bt == 12:
        return "- " + content
    if bt == 13:
        return "1. " + content
    if bt == 14:
        return "```\n" + content + "\n```"
    if bt == 15:
        return "> " + content
    if bt == 17:
        return "- [ ] " + content
    return content


def parse_markdown(md_text):
    blocks = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.strip().startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(text_block(BT_CODE, "\n".join(code_lines)))
            continue
        if line.strip() in ("---", "***", "___"):
            blocks.append(divider_block())
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append(text_block(LEVEL_TO_BT[len(m.group(1))], m.group(2).strip()))
            i += 1
            continue
        m = re.match(r"^[-*+]\s+(.*)$", line)
        if m:
            blocks.append(text_block(BT_BULLET, m.group(1).strip()))
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", line)
        if m:
            blocks.append(text_block(BT_ORDERED, m.group(1).strip()))
            i += 1
            continue
        m = re.match(r"^>\s?(.*)$", line)
        if m:
            blocks.append(text_block(BT_QUOTE, m.group(1).strip()))
            i += 1
            continue
        blocks.append(text_block(BT_TEXT, line))
        i += 1
    return blocks


def resolve_doc_id(arg_doc):
    doc_id = arg_doc or DEFAULT_DOC_ID
    if not doc_id:
        sys.exit("未指定文档：请用 --doc DOCID，或在 .env 设 FEISHU_DOC_ID")
    return doc_id


def cmd_create(args):
    folder = args.folder or FOLDER_TOKEN
    doc_id, url = create_doc(args.title, folder)
    print(f"已创建文档")
    print(f"  document_id : {doc_id}")
    print(f"  url         : {url}")
    if folder:
        print(f"  folder_token: {folder}")
    print(f"\n把 document_id 写进 .env 的 FEISHU_DOC_ID 即可后续直接 append。")


def cmd_append(args):
    doc_id = resolve_doc_id(args.doc)
    blocks = []
    if args.text:
        blocks.append(text_block(BT_TEXT, args.text))
    for h in args.heading or []:
        blocks.append(text_block(LEVEL_TO_BT.get(args.level, 4), h))
    for b in args.bullet or []:
        blocks.append(text_block(BT_BULLET, b))
    for o in args.ordered or []:
        blocks.append(text_block(BT_ORDERED, o))
    for q in args.quote or []:
        blocks.append(text_block(BT_QUOTE, q))
    if args.divider:
        blocks.append(divider_block())
    if args.file:
        md = Path(args.file).read_text(encoding="utf-8")
        blocks.extend(parse_markdown(md))
    if args.stdin:
        md = sys.stdin.read()
        blocks.extend(parse_markdown(md))
    if not blocks:
        sys.exit("没有可追加的内容：请指定 --text/--heading/--bullet/--ordered/--quote/--divider/--file/--stdin")
    n = append_blocks(doc_id, blocks)
    print(f"已向文档追加 {n} 个块")
    print(f"  document_id : {doc_id}")
    print(f"  url         : {DOC_URL_BASE + doc_id}")


def cmd_share(args):
    doc_id = resolve_doc_id(args.doc)
    perm = args.perm or "edit"
    if args.email:
        add_member(doc_id, "email", args.email, perm)
        print(f"已给 {args.email} 授予 {perm} 权限")
    elif args.openid:
        add_member(doc_id, "openid", args.openid, perm)
        print(f"已给 open_id={args.openid} 授予 {perm} 权限")
    elif args.link:
        mapping = {"tenant": "tenant_editable", "anyone": "anyone_editable"}
        entity = mapping[args.link]
        set_link_share(doc_id, entity, external=(args.link == "anyone"))
        scope = "组织内可编辑" if args.link == "tenant" else "任何人可编辑（外部可访问）"
        print(f"已设置链接分享：{scope}")
    else:
        sys.exit("请指定 --email / --openid / --link 之一")
    print(f"  document_id : {doc_id}")
    print(f"  url         : {DOC_URL_BASE + doc_id}")


def cmd_sync(args):
    doc_id = resolve_doc_id(args.doc)
    if args.dir:
        directory = Path(args.dir)
    else:
        directory = Path(__file__).resolve().parent.parent.parent / "knowledge"
    if not directory.is_dir():
        sys.exit(f"目录不存在：{directory}")
    md_files = sorted(directory.glob("*.md"))
    if not md_files:
        sys.exit(f"目录下没有 .md 文件：{directory}")
    blocks = []
    for md_file in md_files:
        blocks.append(divider_block())
        blocks.append(text_block(LEVEL_TO_BT[1], md_file.stem))
        blocks.extend(parse_markdown(md_file.read_text(encoding="utf-8")))
    print(f"将同步 {len(md_files)} 个文件，共 {len(blocks)} 个块 → 文档 {doc_id}")
    for f in md_files:
        print(f"  - {f.name}")
    n = append_blocks(doc_id, blocks)
    print(f"已同步 {n} 个块")
    print(f"  url: {DOC_URL_BASE + doc_id}")


def cmd_view(args):
    if args.wiki:
        node = get_wiki_node(args.wiki)
        print(f"标题     : {node.title}")
        print(f"类型     : {node.obj_type}")
        print(f"obj_token: {node.obj_token}")
        print(f"url      : {node.url}")
        print(f"有子节点 : {node.has_child}")
        print()
        if node.obj_type != "docx":
            print(f"该节点类型为 {node.obj_type}，暂只支持读取 docx 内容。")
            return
        blocks = list_doc_blocks(node.obj_token)
    elif args.doc:
        blocks = list_doc_blocks(args.doc)
    else:
        sys.exit("请指定 --wiki TOKEN 或 --doc DOCID")
    print(f"--- 文档内容（{len(blocks)} 块）---")
    for b in blocks:
        line = render_block(b)
        if line is not None:
            print(line)


def print_children_recursive(space_id, parent_token, depth):
    for ch in list_wiki_children(space_id, parent_token):
        indent = "  " * depth
        mark = "[+]" if ch.has_child else "[-]"
        print(f"{indent}{mark} {ch.title}  ({ch.obj_type})  {ch.node_token}")
        if ch.has_child:
            print_children_recursive(space_id, ch.node_token, depth + 1)


def cmd_children(args):
    node = get_wiki_node(args.wiki)
    print(f"父节点：{node.title}")
    print(f"  space_id={node.space_id}  node_token={node.node_token}")
    print()
    if not node.has_child:
        print("无子节点")
        return
    if args.recursive:
        print_children_recursive(node.space_id, node.node_token, 0)
    else:
        children = list_wiki_children(node.space_id, node.node_token)
        print(f"直接子节点（{len(children)} 个）：")
        for ch in children:
            mark = "[+]" if ch.has_child else "[-]"
            print(f"  {mark} {ch.title}  ({ch.obj_type})  {ch.node_token}")


def clone_block(b):
    bt = b.block_type
    if bt == 22:
        return divider_block()
    field = FIELD_OF_BT.get(bt)
    if not field:
        return None
    t = getattr(b, field, None)
    if not t or not t.elements:
        return None
    contents = [e.text_run.content for e in t.elements if e and e.text_run]
    if not contents:
        return None
    new_text = docx.Text.builder().elements([
        docx.TextElement.builder().text_run(
            docx.TextRun.builder().content("".join(contents)).build()
        ).build()
    ]).build()
    builder = docx.Block.builder().block_type(bt)
    getattr(builder, field)(new_text)
    return builder.build()


def cmd_copy_wiki(args):
    doc_id, url = create_doc(args.title, FOLDER_TOKEN)
    print(f"已创建文档：{args.title}")
    print(f"  document_id: {doc_id}")
    print(f"  url: {url}\n")
    root = get_wiki_node(args.wiki)
    counters = {"nodes": 0, "blocks": 0, "skipped": 0}

    def walk(node, depth):
        if node.obj_type == "docx":
            src_blocks = list_doc_blocks(node.obj_token)
            section = [divider_block()]
            heading_bt = LEVEL_TO_BT.get(min(depth + 1, 6), 8)
            section.append(text_block(heading_bt, node.title or "(无标题)"))
            for b in src_blocks:
                cloned = clone_block(b)
                if cloned is not None:
                    section.append(cloned)
                else:
                    counters["skipped"] += 1
            n = append_blocks(doc_id, section)
            counters["nodes"] += 1
            counters["blocks"] += n
            print(f"{'  ' * depth}[+] {node.title} ({len(src_blocks)} 块)")
        if node.has_child:
            for ch in list_wiki_children(node.space_id, node.node_token):
                walk(ch, depth + 1)

    walk(root, 0)
    print(f"\n已复制 {counters['nodes']} 篇，追加 {counters['blocks']} 块，"
          f"跳过 {counters['skipped']} 个复杂块（表格/图片等）")
    if args.share:
        set_link_share(doc_id, "tenant_editable", external=False)
        print("已设置组织内可编辑")
    print(f"  url: {DOC_URL_BASE + doc_id}")


def build_parser():
    p = argparse.ArgumentParser(
        description="飞书云文档 Docx 连接：创建文档 + 追加内容",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("create", help="创建新文档")
    pc.add_argument("--title", required=True, help="文档标题")
    pc.add_argument("--folder", help="文件夹 token（不传则用 .env 的 FEISHU_FOLDER_TOKEN 或根目录）")
    pc.set_defaults(func=cmd_create)

    pa = sub.add_parser("append", help="向已有文档追加内容")
    pa.add_argument("--doc", help="目标文档 document_id（不传则用 .env 的 FEISHU_DOC_ID）")
    pa.add_argument("--text", help="追加一段正文")
    pa.add_argument("--heading", action="append", help="追加标题（可多次）")
    pa.add_argument("--level", type=int, default=2, choices=[1, 2, 3, 4, 5, 6], help="标题级别，默认 2")
    pa.add_argument("--bullet", action="append", help="追加无序列表项（可多次）")
    pa.add_argument("--ordered", action="append", help="追加有序列表项（可多次）")
    pa.add_argument("--quote", action="append", help="追加引用（可多次）")
    pa.add_argument("--divider", action="store_true", help="追加分割线")
    pa.add_argument("--file", help="把本地 markdown 文件解析后追加")
    pa.add_argument("--stdin", action="store_true", help="从标准输入读 markdown 追加")
    pa.set_defaults(func=cmd_append)

    ps = sub.add_parser("share", help="授权文档（加协作者或开链接分享）")
    ps.add_argument("--doc", help="目标文档 document_id（不传则用 .env 的 FEISHU_DOC_ID）")
    ps.add_argument("--email", help="按邮箱授权")
    ps.add_argument("--openid", help="按 open_id 授权")
    ps.add_argument("--link", choices=["tenant", "anyone"],
                    help="链接分享范围：tenant=组织内可编辑；anyone=任何人可编辑")
    ps.add_argument("--perm", choices=["view", "edit", "full_access"],
                    default="edit", help="协作者权限，默认 edit")
    ps.set_defaults(func=cmd_share)

    py = sub.add_parser("sync", help="把目录下所有 markdown 同步到飞书文档")
    py.add_argument("--dir", help="markdown 目录（默认项目 knowledge/）")
    py.add_argument("--doc", help="目标文档（默认 .env 的 FEISHU_DOC_ID）")
    py.set_defaults(func=cmd_sync)

    pv = sub.add_parser("view", help="查看飞书文档/wiki 内容")
    pv.add_argument("--wiki", help="wiki 节点 token（链接 /wiki/ 后的那段）")
    pv.add_argument("--doc", help="docx document_id")
    pv.set_defaults(func=cmd_view)

    pc = sub.add_parser("children", help="列出 wiki 节点的子节点")
    pc.add_argument("--wiki", required=True, help="wiki 节点 token")
    pc.add_argument("--recursive", action="store_true", help="递归列出所有后代")
    pc.set_defaults(func=cmd_children)

    pw = sub.add_parser("copy-wiki", help="把 wiki 子树复制到一个新 docx 文档")
    pw.add_argument("--wiki", required=True, help="源 wiki 根节点 token")
    pw.add_argument("--title", required=True, help="新文档标题")
    pw.add_argument("--share", action="store_true", help="创建后设置组织内可编辑")
    pw.set_defaults(func=cmd_copy_wiki)
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
