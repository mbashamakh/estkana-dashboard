"""
Maps Loyverse store IDs to the same branch names odoo_pnl.py already uses
(the "odoo_name" key branches/AnalyticMonthly/etc. are all keyed by).

Loyverse's own store `name` field is in Arabic; these were matched by
translation against the 18 real stores returned by GET /stores (see
/api/_diag/loyverse). Every store is an unambiguous 1:1 place-name match
except Khomra -1/-2, which reuses the store-creation-order tie-break the
user already confirmed for the Odoo side (2026-08-16) — Loyverse's
"القرينية _ الخمرة" / "التعاون _ الخمرة ٢" line up with that pairing.
"""
from __future__ import annotations

STORE_ID_TO_ODOO_NAME: dict[str, str] = {
    "aeab39ac-db3b-4d4b-9d7d-9e7b39e0f7df": "Alsamer",     # استكانة فرع - السامر
    "1af1c515-e975-45fd-8c2d-b0f8348a687b": "Zahra",        # استكانه فرع - الزهراء
    "c3734202-34cb-4442-999e-a9d48c9ddf32": "slumaniah",    # استكانة فرع _ السليمانية
    "e6d7c501-9e73-4adb-9496-ac446e1fd4be": "NASEEM 2",     # استكانة فرع - النسيم ٢
    "d6f61b67-9bea-424e-9280-c7972a92ef51": "NASEEM 1",     # استكانة فرع - النسيم
    "63dfe365-cb85-4bb5-b1f1-d18735f77a52": "SAFWA",        # استكانة فرع الصفوة
    "80bd2f72-be7e-4ef8-b747-168321bc6027": "Madian",       # استكانة فرع المدينة
    "ef390e06-47ab-4a4e-b177-ae25f7486dcb": "FOROSIA",      # استكانة فرع الفروسية
    "b72d170d-0546-4132-8923-931229dee5ea": "SAFA",         # استكانة فرع الصفا
    "86ee515c-ae10-4fff-a141-e97f94ccc919": "SASCO",        # استكانة فرع ساسكو
    "629660f7-744a-4891-914d-91b49d3b3a93": "NOZHA",        # استكانة فرع النزهة
    "a06289ee-62c3-4cf9-ac13-6e9396082f2b": "SHARKIA",      # استكانة فرع الشرقية الريان
    "6a6485e1-80bc-4b33-8a41-b224c5d488f9": "ARBEEN",       # ARBEEN
    "f8990cfb-b43d-44ae-8dd3-cd16f9a9be1f": "HERAA",        # استكانة فرع حراء
    "3481dfc7-1c89-4558-bd98-63db777ae589": "FALSTEEN",     # استكانة فرع فلسطين
    "b353fb3c-cc47-492f-9db1-5eb2da39cb0d": "HAMDANEYA",    # استكانة فرع الحمدانية
    "6e15c4a0-a7e2-4af0-94d6-e363a736c8d4": "Khomra -1",    # استكانة فرع القرينية _ الخمرة
    "3f9d7722-af3c-4932-ab2c-493f27b6a61c": "Khomra -2",    # استكانة فرع التعاون _ الخمرة ٢
}
