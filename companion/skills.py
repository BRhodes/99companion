"""Skill ids and the server's skill-usage XP grant rules.

Mirrors CPlayerWeenie::MaybeGiveSkillUsageXP (Server/Source/Player.cpp:6237):
  * instant grant when difficulty > stored difficulty + 2
  * otherwise, after 60 s since the last grant, chance = (elapsed - 60) / 900 per check
  * skill XP = difficulty * U(0.75, 1.25); unassigned XP = difficulty // 10
  * (bug) the skill XP is also added to unassigned XP, so unassigned rises by skill_xp + difficulty // 10
"""

SKILL_NAMES = {
    1: "Axe", 2: "Bow", 3: "Crossbow", 4: "Dagger", 5: "Mace", 6: "Melee Defense",
    7: "Missile Defense", 8: "Sling", 9: "Spear", 10: "Staff", 11: "Sword", 12: "Thrown Weapons",
    13: "Unarmed Combat", 14: "Arcane Lore", 15: "Magic Defense", 16: "Mana Conversion",
    17: "Spellcraft", 18: "Assess Item", 19: "Assess Person", 20: "Deception", 21: "Healing",
    22: "Jump", 23: "Lockpick", 24: "Run", 25: "Awareness", 26: "Arms and Armor Repair",
    27: "Assess Creature", 28: "Assess Weapon", 29: "Assess Armor", 30: "Assess Magic Item",
    31: "Creature Enchantment", 32: "Item Enchantment", 33: "Life Magic", 34: "War Magic",
    35: "Leadership", 36: "Loyalty", 37: "Fletching", 38: "Alchemy", 39: "Cooking",
}

MANA_CONVERSION = 16

# SKILL_ADVANCEMENT_CLASS
SAC_NAMES = {0: "Unusable", 1: "Untrained", 2: "Trained", 3: "Specialized"}
TRAINED = 2

MIN_WAIT = 60.0
MAX_WAIT = 900.0
HARDER_BY = 2  # instant grant needs difficulty > stored + 2


def skill_name(skill_id):
    return SKILL_NAMES.get(skill_id, f"Skill {skill_id}")


def timed_grant_chance(seconds_since_grant):
    """Per-check probability of a timed grant."""
    if seconds_since_grant <= MIN_WAIT:
        return 0.0
    return min(1.0, (seconds_since_grant - MIN_WAIT) / MAX_WAIT)


def instant_threshold(stored_difficulty):
    """Smallest difficulty that grants instantly."""
    return stored_difficulty + HARDER_BY + 1


def mana_conversion_difficulty(spell_power):
    """SpellcastingManager.cpp:1087. C++ round() rounds halves away from zero."""
    x = ((50 + spell_power) / 50.0 + 1) * 25.0
    return int(x + 0.5)


def difficulty_from_deltas(unassigned_delta, skill_delta):
    """Estimate a grant's difficulty from XP deltas under the double-count bug.

    unassigned rises by skill_xp + difficulty // 10, so difficulty // 10 == unassigned_delta - skill_delta.
    Returns (low, high) or None if the deltas don't fit that shape.
    """
    tenth = unassigned_delta - skill_delta
    if tenth < 0:
        return None
    return (tenth * 10, tenth * 10 + 9)
