# -*- coding: utf-8 -*-
"""注册资料生成（生日、显示名）。"""
from __future__ import annotations

import random
from datetime import date, timedelta

_FIRST_NAMES = [
    "James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Lucas", "Mia",
    "Ethan", "Sofia", "Mason", "Isabella", "Logan", "Charlotte", "Jack",
    "Amelia", "Owen", "Harper", "Henry", "Evelyn", "Leo", "Grace",
    "Daniel", "Chloe", "Aaron", "Lily", "Adam", "Zoe", "Felix", "Nora",
    "Hugo", "Iris", "Max", "Ruby", "Oscar", "Alice", "Theo", "Clara",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Brown", "Taylor", "Miller", "Davis", "Wilson",
    "Anderson", "Thomas", "Moore", "Martin", "Lee", "Walker", "Hall",
    "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams",
    "Nelson", "Carter", "Mitchell", "Turner", "Phillips", "Campbell",
    "Parker", "Evans", "Edwards", "Collins", "Stewart", "Morris",
]


def _shift_year_safe(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def generate_random_birthday(min_age: int = 18, max_age: int = 65) -> str:
    if min_age < 0 or max_age < min_age:
        raise ValueError(f"年龄范围无效: {min_age}, {max_age}")
    today = date.today()
    oldest = _shift_year_safe(today, -max_age)
    youngest = _shift_year_safe(today, -min_age)
    span_days = (youngest - oldest).days
    return (oldest + timedelta(days=random.randint(0, span_days))).isoformat()


def random_display_name() -> str:
    """OpenAI 限制 name_invalid_chars：只允许字母和空格。"""
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"
