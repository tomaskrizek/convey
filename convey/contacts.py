import csv
import os
import re
from typing import Dict

import lepl.apps.rfc3696

from .config import Config
from .mailDraft import MailDraft


class Attachment:
    # XXpython3.6 sent: bool  # True => sent, False => error while sending, None => not yet sent
    # XXpython3.6 partner: bool  # True => partner e-mail is in Contacts dict, False => e-mail is file name, None => undeliverable (no e-mail)

    def __init__(self, partner, sent, path):
        self.partner = partner
        self.sent = sent
        self.path = path

    @classmethod
    def get_basic(cls, attachments):
        return cls._get(attachments, False)

    @classmethod
    def get_partner(cls, attachments):
        return cls._get(attachments, True)

    @classmethod
    def _get(cls, attachments, listed_only=False):
        for o in attachments:
            if o.path in [Config.UNKNOWN_NAME, Config.INVALID_NAME]:
                continue

            cc = ""

            if listed_only:
                if o.path in Contacts.countrymails:
                    mail = Contacts.countrymails[o.path]
                else:  # we don't want send to standard abuse mail, just to a partner
                    continue
            else:
                mail = o.path

            for domain in Contacts.getDomains(mail):
                if domain in Contacts.abusemails:
                    cc += Contacts.abusemails[domain] + ";"

            try:
                with open(Config.getCacheDir() + o.path, "r") as f:
                    yield o, mail, cc, f.read()
            except FileNotFoundError:
                continue

    @classmethod
    def refresh_attachment_stats(cls, csv):
        attachments = csv.attachments
        st = csv.stats
        email_validator = lepl.apps.rfc3696.Email()
        st["partner_count"] = [0, 0]
        st["abuse_count"] = [0, 0]
        st["non_deliverable"] = 0
        st["totals"] = 0

        for o in attachments:
            if o.path in Contacts.countrymails:
                st["partner_count"][int(bool(o.sent))] += 1
                o.partner = True
            elif email_validator(o.path):
                st["abuse_count"][int(bool(o.sent))] += 1
                o.partner = False
            else:
                st["non_deliverable"] += 1
                o.partner = None
            st["totals"] += 1


class Contacts:
    # XXpython3.6 abusemails: Dict[str, str]
    # XXpython3.6 countrymails: Dict[str, str]

    @classmethod
    def init(cls):
        cls.mailDraft = {"local": MailDraft("mail_template_local"), "foreign": MailDraft("mail_template_foreign")}
        cls.abusemails = cls._update("contacts_local")
        cls.countrymails = cls._update("contacts_foreign")

    @staticmethod
    def getDomains(mailStr):
        """ mail = mail@example.com;mail2@example2.com -> [example.com, example2.com] """
        try:
            # return set(re.findall("@([\w.]+)", mail))
            return set([x[0] for x in re.findall("@(([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,6})", mailStr)])
        except AttributeError:
            return []

    @staticmethod
    def _update(key: Dict[str, str]) -> object:
        """ Update info from an external CSV file. """
        file = Config.get(key)
        if not os.path.isfile(file):  # file with contacts
            print("(Contacts file {} not found on path {}/{}.) ".format(key, os.getcwd(),file))
            return {}
        else:
            with open(file, 'r') as csvfile:
                reader = csv.reader(csvfile)
                rows = {rows[0]: rows[1] for rows in reader}
                return rows
