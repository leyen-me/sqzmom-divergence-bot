"""QQ 邮箱异步通知: 后台线程 + 队列, 不阻塞交易主循环。

环境变量:
  MSG_SMTP_USERNAME  发件邮箱(如 123456@qq.com), 别名 QQ_SMTP_USER
  MSG_SMTP_PASSWORD  SMTP 授权码,             别名 QQ_SMTP_SECRET
  QQ_NOTIFY_TO       收件邮箱(默认发件邮箱自身)
  ENABLE_EMAIL_MSG   true/false (默认 true)
"""

import os
import queue
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))


class Mailer:
    def __init__(self, log=print):
        self.log = log
        self.user = os.environ.get("MSG_SMTP_USERNAME") or os.environ.get("QQ_SMTP_USER", "")
        self.secret = os.environ.get("MSG_SMTP_PASSWORD") or os.environ.get("QQ_SMTP_SECRET", "")
        self.to_addr = os.environ.get("QQ_NOTIFY_TO") or self.user
        flag = os.environ.get("ENABLE_EMAIL_MSG", "true").strip().lower()
        self.enabled = bool(self.user and self.secret) and flag in ("1", "true", "yes", "on")
        self._q = queue.Queue(maxsize=100)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="okx-mailer").start()

    def send(self, subject, body=""):
        if not self.enabled:
            self.log("[mail:off] %s | %s" % (subject, body.replace("\n", " ")[:160]))
            return
        try:
            self._q.put_nowait((subject[:120], body))
        except queue.Full:
            self.log("[mail] 队列已满, 丢弃: %s" % subject)

    def _worker(self):
        while True:
            subject, body = self._q.get()
            for attempt in range(3):
                try:
                    self._smtp_send(subject, body)
                    self.log("[mail] 已发送: %s" % subject)
                    break
                except Exception as e:
                    self.log("[mail] 发送失败(第%d次): %s | %s" % (attempt + 1, subject, e))
                    threading.Event().wait(3 * (attempt + 1))
            else:
                self.log("[mail] 最终发送失败: %s" % subject)

    def _smtp_send(self, subject, body):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user
        msg["To"] = self.to_addr
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(
            domain="qq.com", idstring=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        msg.set_content(body or subject, charset="utf-8")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(self.user, self.secret)
            s.send_message(msg)
