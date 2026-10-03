"""邮件（`.eml` / `.msg`）→ 文字 + 附件递归（P3）。

`.eml` 是**纯文本格式**，用标准库 `email` 就能完整解析（不需要任何依赖）；
`.msg` 是 Outlook 的 **OLE2 复合文档**，两级：

  ① `extract-msg`（最稳，装了就优先用它）；
  ② `olefile` 直接读它的 `__substg1.0_*` 属性流（只取主题/正文/收发件人/附件名 —— **够用但有限**）；
  ③ 都没有 → 如实说缺什么、怎么装。

**附件一定会递归读**：附件是"别人在邮件里塞给我的文件"，它的内容和正文一样值得读；
正文里能读到的那些也**标明来源**（第几个附件、叫什么名字）。
"""
import email
import email.policy
import os
import tempfile

import tempdir

_HERE = os.path.dirname(os.path.abspath(__file__))

MAIL_EXT = (".eml", ".msg", ".mbox")


def is_eml(path):
    return str(path or "").lower().endswith(".eml")


def _cfg(cfg):
    sec = ((cfg or {}).get("mail") or {})
    return dict(sec) if isinstance(sec, dict) else {}


def max_attachments(cfg=None):
    try:
        n = int(_cfg(cfg).get("max_attachments") or 5)
    except (TypeError, ValueError):
        n = 5
    return max(0, min(n, 50))


def _decode_part(part):
    """把一个 MIME 部分的正文解出来（**按它声明的字符集**解，不许硬按 utf-8）。"""
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def read_eml(path, cfg=None, on_image=None):
    """读一封 `.eml`。返回 `(文本, 错误)`。附件递归交给 `file_read.extract`。"""
    import file_read
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)
    except Exception as e:
        return None, f"这封邮件打不开（{type(e).__name__}: {str(e)[:120]}）"

    lines = ["—— 邮件 ——"]
    for label, key in (("发件人", "From"), ("收件人", "To"), ("抄送", "Cc"),
                       ("主题", "Subject"), ("时间", "Date")):
        v = msg.get(key)
        if v:
            lines.append(f"{label}：{v}")

    bodies, attachments = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        ctype = (part.get_content_type() or "").lower()
        if fname or ctype.startswith(("application/", "image/", "audio/", "video/")):
            attachments.append((fname or f"未命名.{ctype.split('/')[-1]}", part))
            continue
        if ctype == "text/plain":
            bodies.append(_decode_part(part))
        elif ctype == "text/html":
            # HTML 正文只做最朴素的标签剥离：**够看**，但要说清这是剥过的
            import re
            html = _decode_part(part)
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            bodies.append("（HTML 正文，已剥掉标签）\n" + text.strip())

    body = "\n\n".join(b.strip() for b in bodies if b.strip())
    if body:
        lines.append("\n【正文】\n" + body[:20000])
    else:
        lines.append("（这封邮件没有 text/plain 正文）")

    if attachments:
        lines.append(f"\n【附件 {len(attachments)} 个】")
        limit = max_attachments(cfg)
        d = tempdir.get("tmp_mail")
        for i, (name, part) in enumerate(attachments):
            if i >= limit:
                lines.append(f"  · {name}（**没读**：超过 mail.max_attachments={limit}）")
                continue
            data = part.get_payload(decode=True) or b""
            if not data:
                lines.append(f"  · {name}（空的）")
                continue
            tmp = os.path.join(d, f"{i:02d}_{os.path.basename(name)[:60]}")
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                text, err = file_read.extract(tmp, cfg, on_image=on_image)
                if err:
                    lines.append(f"  · {name}（{len(data)} 字节）读不了：{err}")
                elif text:
                    lines.append(f"  · {name}（{len(data)} 字节）：\n{text}")
                else:
                    lines.append(f"  · {name}（{len(data)} 字节）：没抽出文字")
            finally:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
    return "\n".join(lines), None


# ---------------- .msg（Outlook OLE2） ----------------
_MSG_PROPS = {
    "0037001E": "主题", "0037001F": "主题",
    "1000001E": "正文", "1000001F": "正文",
    "0C1A001E": "发件人", "0C1A001F": "发件人",
    "0C1F001E": "发件人地址", "0C1F001F": "发件人地址",
    "0E04001E": "收件人", "0E04001F": "收件人",
    "0039001E": "时间", "00390040": "时间",
}


def msg_via_extract_msg(path):
    try:
        import extract_msg
    except ImportError:
        return None, "没装 extract-msg"
    try:
        m = extract_msg.Message(path)
    except Exception as e:
        return None, f"extract-msg 打不开：{type(e).__name__}: {str(e)[:120]}"
    try:
        head = [f"发件人：{m.sender}", f"收件人：{m.to}", f"主题：{m.subject}",
                f"时间：{m.date}"]
        body = (m.body or "").strip()
        out = ["—— Outlook 邮件（extract-msg）——"]
        out += [h for h in head if not h.endswith("None")]
        out.append("\n【正文】\n" + (body[:20000] if body else "（没有正文）"))
        names = []
        for a in (m.attachments or []):
            nm = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None)
            names.append(str(nm or "未命名附件"))
        if names:
            out.append(f"\n【附件 {len(names)} 个】" + "".join(f"\n  · {n}" for n in names))
            out.append("（附件内容没读：extract-msg 那条路只列名字）")
        return "\n".join(out), None
    finally:
        try:
            m.close()
        except Exception:
            pass


def msg_via_olefile(path):
    """退路：直接读 `.msg` 的 `__substg1.0_*` 流。

    `.msg` 的属性流名有固定套路：`__substg1.0_<4 位属性 ID><4 位类型码>`，
    类型码 `001F` = UTF-16LE 字符串、`001E` = 单字节(ANSI) 字符串。
    **只取几个关键属性**（主题/正文/收发件人/时间）—— 比一句"读不了"强，
    但要如实说这是**有限**的（附件、会话线程、rtf 正文都拿不到）。
    """
    try:
        import olefile
    except ImportError:
        return None, ("读 .msg 需要 extract-msg 或 olefile，都没装："
                      "`.venv\\Scripts\\python.exe -m pip install extract-msg`"
                      "（或 pip install olefile，但只能取到主题/正文这些关键字段）")
    if not olefile.isOleFile(path):
        return None, "这不是 OLE2 复合文档（.msg 应该是）"
    try:
        ole = olefile.OleFileIO(path)
        try:
            found = {}
            for entry in ole.listdir():
                nm = "/".join(entry)
                if not nm.startswith("__substg1.0_"):
                    continue
                key = nm[len("__substg1.0_"):].upper()
                label = _MSG_PROPS.get(key)
                if not label or label in found:
                    continue
                data = ole.openstream(entry).read()
                if key.endswith("001F"):
                    text = data.decode("utf-16-le", "ignore").rstrip("\x00")
                else:
                    text = data.decode("gb18030", "replace").rstrip("\x00")
                text = text.strip()
                if text:
                    found[label] = text
        finally:
            ole.close()
    except Exception as e:
        return None, f"olefile 读不动：{type(e).__name__}: {str(e)[:120]}"

    if not found:
        return None, ("这个 .msg 里没找到可读的属性流（可能不是标准 .msg，"
                      "或格式比较特殊）；装 extract-msg 会更稳。")
    order = ["主题", "发件人", "发件人地址", "收件人", "时间", "正文"]
    out = ["—— Outlook 邮件（olefile 退路，**只取到关键字段**）——"]
    for k in order:
        if k in found:
            body = found[k]
            if k == "正文":
                out.append("\n【正文】\n" + body[:20000])
            else:
                out.append(f"{k}：{body[:300]}")
    out.append("（⚠️ 这条退路**拿不到附件、也拿不到 RTF 正文**；要看全就装 extract-msg。）")
    return "\n".join(out), None


def read_msg(path, cfg=None, on_image=None):
    """读 `.msg`。返回 `(文本, 错误)`。先试 extract-msg，再退到 olefile，最后如实说。"""
    notes = []
    text, err = msg_via_extract_msg(path)
    if text:
        return text, None
    notes.append(f"extract-msg：{err}")
    text2, err2 = msg_via_olefile(path)
    if text2:
        return text2 + "\n（前面的引擎：" + "；".join(notes) + "）", None
    notes.append(f"olefile：{err2}")
    return None, ("这封 .msg 读不了——本机没有一个能读它的引擎：\n  · " + "\n  · ".join(notes)
                  + "\n要读它：`.venv\\Scripts\\python.exe -m pip install extract-msg`\n"
                    "**请如实告诉用户读不了，不要编内容。**")


def sweep_tmp(max_age=86400.0):
    """清理邮件附件留下的临时文件（**删了什么要打日志**，由 `tempdir.sweep` 统一实现）。"""
    return tempdir.sweep("tmp_mail", max_age, "mail_read")
