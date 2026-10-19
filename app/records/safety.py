"""Requests Nova Records refuses before anything is planned or read (BE-9 §8).

Deterministic on purpose: whether the assistant may change data, send a message, reveal
identity documents or its own instructions is not a question for a model. Only the
EMPLOYEE'S QUESTION is screened here — text inside records is data and never decides
anything (it is quoted as data in both prompts and checked by the verifier).
"""
from __future__ import annotations

import re
from typing import Optional

WRITE = "write_request"
SEND = "send_request"
IDENTITY = "identity_data"
SYSTEM = "system_prompt"

_LEAD_IN = r"^\s*(?:(?:hey|hi|ok|okay)\s+)?(?:nova[,:]?\s+)?(?:(?:please|pls|kindly)\s+)?" \
           r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need)\s+you\s+to\s+|go\s+ahead\s+and\s+)?"
_WRITE_VERBS = (r"change|update|edit|modify|correct|fix|delete|remove|erase|purge|void|cancel|assign|reassign|"
                r"unassign|approve|reject|verify|mark|record|log|create|add|insert|register|set|reset|move|advance|"
                r"close|reopen|resolve|refund|pay|release|discount|reprice|re-price|price|lock|unlock|upload|"
                r"import|merge|rename|overwrite|enter|draft")
_SEND_VERBS = r"send|email|e-mail|mail|message|text|sms|whatsapp|call|ring|notify|remind|share|forward|book|schedule|invite"
# The verb must be an instruction, not a noun: "Schedule of payments…", "Price for 5 kW?",
# "Share of projects by district" are questions.
_NOT_AN_OBJECT = r"(?!\s+(?:of|for|in|on|by|per|from|between|among|across|is|are|was|were|status|history|list)\b)" \
                 r"(?!\s*[?]|\s*$)"
_RE_WRITE = re.compile(_LEAD_IN + rf"(?:{_WRITE_VERBS})\b" + _NOT_AN_OBJECT, re.I)
_RE_SEND = re.compile(_LEAD_IN + rf"(?:{_SEND_VERBS})\b" + _NOT_AN_OBJECT, re.I)
# "… and send it to him", "then mark it paid" — an instruction anywhere in the question.
_RE_TRAILING = re.compile(rf"\b(?:and|then|also)\s+(?:please\s+)?(?:{_SEND_VERBS}|{_WRITE_VERBS})\s+"
                          r"(?:it|them|this|that|the|him|her|a|an|me|us)\b", re.I)
_RE_IDENTITY = re.compile(
    r"\b(?:aadhaa?r|uidai|pan(?:\s*(?:card|number|no\.?|details))?|permanent account number|bank\s*(?:account|a/c|"
    r"details|number|statement)|account\s*(?:number|no\.?)|ifsc|passport|voter\s*id|driving\s*licen[cs]e|kyc|"
    r"identity\s*(?:document|proof|card)s?|id\s*(?:proof|document|card)s?|photo\s*id)\b", re.I)
_RE_PAN_ALONE = re.compile(r"\bpan\b", re.I)
_RE_SYSTEM = re.compile(
    r"\b(?:system\s*prompt|your\s+(?:instructions|prompt|rules|guidelines|configuration)|developer\s+message|"
    r"ignore\s+(?:all\s+|your\s+|the\s+)?(?:previous|prior|above)|jailbreak|reveal\s+(?:your|the)\s+prompt|"
    r"what\s+(?:are|were)\s+you\s+told|act\s+as\s+(?:an?\s+)?(?:admin|developer|dan))\b", re.I)
# A "pan" that is not a PAN number (solar pan, pan-India).
_RE_PAN_OK = re.compile(r"\bpan[-\s]+india\b", re.I)

ANSWERS = {
    WRITE: "I can only read records — I can't change, add, assign, approve or delete anything. "
           "Open the record in the dashboard to make that change.",
    SEND: "I can't send messages, emails or invitations, or book anything — I only read records. "
          "Use the dashboard to contact the customer or schedule a meeting.",
    IDENTITY: "I never show Aadhaar, PAN, bank details or identity documents. Authorised staff can see the "
              "masked values on the record itself.",
    SYSTEM: "I can't share my instructions. Ask me about projects, leads, customers, payments or warranties "
            "on record.",
}


def screen(question: str) -> Optional[str]:
    """The refusal category for `question`, or None when it may be answered."""
    q = question or ""
    if _RE_SYSTEM.search(q):
        return SYSTEM
    if _RE_IDENTITY.search(q) and not (_RE_PAN_ALONE.search(q) and _RE_PAN_OK.search(q)
                                       and not _RE_IDENTITY.search(_RE_PAN_OK.sub(" ", q))):
        return IDENTITY
    if _RE_SEND.search(q):
        return SEND
    if _RE_WRITE.search(q):
        return WRITE
    if m := _RE_TRAILING.search(q):
        return SEND if re.match(rf"(?:and|then|also)\s+(?:please\s+)?(?:{_SEND_VERBS})", m.group(0), re.I) else WRITE
    return None
