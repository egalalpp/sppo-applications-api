"""Справочник оснований (коллекция application_types) — 13 бланков СППО.

Таблица из schema_zayavok_sppo.md, раздел 2. Слот с несколькими any_of
выражает формулировку «ИЛИ» из бланка.
"""

COMMON_SLOTS = [
    {"slot": "passport", "any_of": ["passport_registration"]},
    {"slot": "union_card", "any_of": ["union_card"]},
    {"slot": "bank", "any_of": ["bank_requisites_screenshot"]},
]

BIRTH_STUDENT = {"slot": "birth", "any_of": ["birth_certificate_student"]}


def _basis(slot_types: list[str]) -> dict:
    return {"slot": "basis_doc", "any_of": slot_types}


_TYPES = [
    ("orphan", 1, "Студент-сирота",
     [BIRTH_STUDENT, _basis(["death_certificate", "parental_rights_deprivation"])], []),
    ("disabled", 2, "Студент-инвалид",
     [_basis(["disability_certificate"])], []),
    ("has_child", 3, "Студент, воспитывающий ребёнка",
     [{"slot": "child_birth", "any_of": ["birth_certificate_child"]}], []),
    ("single_parent", 4, "Студент из семьи родителя-одиночки",
     [BIRTH_STUDENT, _basis(["paternity_basis_certificate"])], []),
    ("breadwinner_loss", 5, "Студент, потерявший кормильца",
     [BIRTH_STUDENT, _basis(["death_certificate", "missing_person_court_decision"])], []),
    ("parents_disabled_or_pensioners", 6, "Родители — инвалиды или пенсионеры",
     [BIRTH_STUDENT, _basis(["pension_certificate_parents"])], []),
    ("large_family", 7, "Студент из многодетной семьи",
     [BIRTH_STUDENT, _basis(["large_family_certificate", "family_composition_certificate"])], []),
    ("combat_veteran", 8, "Участник военных действий",
     [{"slot": "service", "any_of": ["military_service_certificate"]},
      {"slot": "military_id", "any_of": ["military_id"]}], []),
    ("dispensary", 9, "Студент на диспансерном учёте",
     [{"slot": "medical", "any_of": ["medical_certificate"]}], ["doctor_certified"]),
    ("chernobyl", 10, "Студент-чернобылец",
     [_basis(["chernobyl_certificate"])], []),
    ("parents_divorced", 11, "Студент из семьи разведённых родителей",
     [BIRTH_STUDENT, _basis(["divorce_certificate"])], []),
    ("dormitory", 12, "Студент, проживающий в общежитии",
     [], ["dorm_head_signed"]),
    ("no_stipend", 13, "Студент, не получающий стипендию",
     [{"slot": "grades", "any_of": ["grades_extract"], "note": "для студентов-бюджетников"}], []),
]

BENEFIT_TYPES: list[dict] = [
    {
        "_id": code,
        "form_no": form_no,
        "title": title,
        "requires": COMMON_SLOTS + extra,
        "manual_flags": manual_flags,
        "is_active": True,
    }
    for code, form_no, title, extra, manual_flags in _TYPES
]
