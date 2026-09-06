"""Seed reference data: agents, expense categories, fact predicates.

Reference data lives in a migration rather than a seed script so that a fresh
`alembic upgrade head` produces a working system. Categories carry keyword lists
used by the deterministic categoriser, which handles the overwhelming majority
of daily logging without a model call — cheaper, faster, and reproducible.

Revision ID: 0002
Revises: 0001
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AGENTS = [
    ("astraea", "Astraea", "global"),
    ("lyra", "Lyra", "health"),
    ("vega", "Vega", "finance"),
    ("nova", "Nova", "work"),
    ("athena", "Athena", "learning"),
    ("selene", "Selene", "home"),
]

# (slug, display, parent, keywords, is_essential, sort)
# Keywords are lowercase substrings matched against the transcript. Tuned for
# Indian daily spend — "auto", "ola", "kirana", "swiggy" carry most of the load.
CATEGORIES = [
    ("food",          "Food",              None,   [], True,  10),
    ("groceries",     "Groceries",         "food", ["grocery", "groceries", "kirana",
                                                    "supermarket", "bigbasket", "blinkit",
                                                    "zepto", "dmart", "vegetables", "sabzi"],
                                                    True,  11),
    ("dining_out",    "Dining out",        "food", ["restaurant", "dinner out", "lunch out",
                                                    "cafe", "coffee", "chai", "tea",
                                                    "breakfast out"], False, 12),
    ("food_delivery", "Food delivery",     "food", ["swiggy", "zomato", "delivery",
                                                    "ordered in", "ordered food"], False, 13),

    ("transport",     "Transport",         None,   [], True,  20),
    ("auto_taxi",     "Auto & taxi",       "transport", ["auto", "rickshaw", "ola", "uber",
                                                         "rapido", "taxi", "cab"], False, 21),
    ("fuel",          "Fuel",              "transport", ["petrol", "diesel", "fuel", "gas"],
                                                         False, 22),
    ("public_transit","Public transport",  "transport", ["metro", "bus", "train", "irctc"],
                                                         False, 23),

    ("housing",       "Housing",           None,   [], True,  30),
    ("rent",          "Rent",              "housing", ["rent"], True, 31),
    ("utilities",     "Utilities",         "housing", ["electricity", "water bill", "gas bill",
                                                       "internet", "broadband", "wifi",
                                                       "maintenance"], True, 32),
    ("household",     "Household",         "housing", ["cleaning", "detergent", "repairs",
                                                       "plumber", "electrician", "carpenter"],
                                                       False, 33),

    ("health",        "Health",            None,   ["medicine", "pharmacy", "doctor", "clinic",
                                                    "hospital", "medical", "chemist"],
                                                    True,  40),
    ("fitness",       "Fitness",           "health", ["gym", "protein", "supplement",
                                                      "whey", "creatine", "trainer"], False, 41),

    ("subscriptions", "Subscriptions",     None,   ["subscription", "netflix", "spotify",
                                                    "prime", "youtube premium", "icloud",
                                                    "github", "openai", "claude"], False, 50),
    ("shopping",      "Shopping",          None,   ["amazon", "flipkart", "myntra", "clothes",
                                                    "shoes", "bought"], False, 60),
    ("education",     "Education",         None,   ["course", "book", "books", "exam fee",
                                                    "gre", "toefl", "ielts", "application fee",
                                                    "tuition"], False, 70),
    ("personal_care", "Personal care",     None,   ["haircut", "salon", "grooming"], False, 80),
    ("entertainment", "Entertainment",     None,   ["movie", "cinema", "concert", "game",
                                                    "steam"], False, 90),
    ("gifts",         "Gifts & giving",    None,   ["gift", "donation", "charity"], False, 95),
    ("uncategorised", "Uncategorised",     None,   [], False, 999),
]

# (slug, namespace, cardinality, value_type, description)
# Cardinality is what makes conflict detection mechanical. See memory-design.md §4.
PREDICATES = [
    # global
    ("full_name",            "global",   "single", "string", "Legal or preferred full name"),
    ("city",                 "global",   "single", "string", "Current city of residence"),
    ("timezone",             "global",   "single", "string", None),
    ("occupation",           "global",   "single", "string", None),
    ("long_term_goal",       "global",   "multi",  "string", "Standing multi-month objective"),
    ("important_person",     "global",   "multi",  "json",   "Name, relationship, key dates"),
    ("routine",              "global",   "multi",  "string", "Habitual pattern, e.g. gym at 6am"),
    ("preference",           "global",   "multi",  "string", "Stated preference of any kind"),

    # finance
    ("monthly_rent",         "finance",  "single", "money",  None),
    ("monthly_income",       "finance",  "single", "money",  None),
    ("rent_due_day",         "finance",  "single", "number", "Day of month rent is due"),
    ("savings_target",       "finance",  "single", "money",  None),
    ("bank_account",         "finance",  "multi",  "string", "Institution only — never numbers"),
    ("financial_goal",       "finance",  "multi",  "string", None),

    # health
    ("height_cm",            "health",   "single", "number", None),
    ("target_weight_kg",     "health",   "single", "number", None),
    ("daily_protein_target", "health",   "single", "number", "Grams per day"),
    ("daily_calorie_target", "health",   "single", "number", None),
    ("dietary_restriction",  "health",   "multi",  "string", "Vegetarian, allergy, etc."),
    ("training_split",       "health",   "single", "string", None),
    ("injury",               "health",   "multi",  "json",   "Body part, status, since"),

    # work
    ("active_project",       "work",     "multi",  "string", None),
    ("tech_preference",      "work",     "multi",  "string", None),
    ("work_hours",           "work",     "single", "string", "Typical productive window"),

    # learning
    ("target_intake",        "learning", "single", "string", "e.g. Fall 2028"),
    ("target_university",    "learning", "multi",  "string", None),
    ("exam_date",            "learning", "multi",  "json",   "Exam name and date"),
    ("current_track",        "learning", "multi",  "string", "Active curriculum track"),
    ("study_window",         "learning", "single", "string", "When study reliably happens"),

    # home
    ("home_address",         "home",     "single", "string", None),
    ("landlord_contact",     "home",     "single", "string", None),
    ("document_expiry",      "home",     "multi",  "json",   "Document kind and expiry date"),
    ("recurring_chore",      "home",     "multi",  "json",   "Chore and cadence"),
]


def upgrade() -> None:
    op.bulk_insert(
        sa.table(
            "agents",
            sa.column("name", sa.String),
            sa.column("display_name", sa.String),
            sa.column("namespace", sa.String),
        ),
        [{"name": n, "display_name": d, "namespace": ns} for n, d, ns in AGENTS],
    )

    # keywords is declared Text rather than JSON here so that `alembic upgrade
    # --sql` can render it as a literal. Postgres casts the JSON string on insert
    # into the JSONB column, so online and offline runs produce the same rows.
    cat_table = sa.table(
        "expense_categories",
        sa.column("slug", sa.String),
        sa.column("display_name", sa.String),
        sa.column("parent", sa.String),
        sa.column("keywords", sa.Text),
        sa.column("is_essential", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    # Parents first — the table is self-referential.
    op.bulk_insert(cat_table, [
        {"slug": s, "display_name": d, "parent": p, "keywords": json.dumps(k),
         "is_essential": e, "sort_order": o}
        for s, d, p, k, e, o in CATEGORIES if p is None
    ])
    op.bulk_insert(cat_table, [
        {"slug": s, "display_name": d, "parent": p, "keywords": json.dumps(k),
         "is_essential": e, "sort_order": o}
        for s, d, p, k, e, o in CATEGORIES if p is not None
    ])

    op.bulk_insert(
        sa.table(
            "fact_predicates",
            sa.column("slug", sa.String),
            sa.column("namespace", sa.String),
            sa.column("cardinality", sa.String),
            sa.column("value_type", sa.String),
            sa.column("description", sa.Text),
        ),
        [{"slug": s, "namespace": ns, "cardinality": c, "value_type": v, "description": d}
         for s, ns, c, v, d in PREDICATES],
    )


def downgrade() -> None:
    op.execute("DELETE FROM fact_predicates")
    op.execute("DELETE FROM expense_categories")
    op.execute("DELETE FROM agents")
