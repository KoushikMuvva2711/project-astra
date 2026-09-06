"""Seed the food table with common Indian portions.

Values are per *standard spoken portion* — one roti, one katori, one glass —
because that is how the user will actually log. A nutrition coach that demands
grams for "two rotis and dal" is useless in this kitchen.

These figures are approximations drawn from common composition tables and vary
with recipe, oil, and portion size; a home-made dal is not a restaurant dal.
That is acceptable and is marked on every derived row via `meal_items.estimated`,
so Lyra can say "roughly" rather than implying a precision she does not have.
Anything the user weighs themselves can be corrected and stored exactly.

Units: macros in decigrams (3.4 g -> 34) so totals stay integer arithmetic.

Revision ID: 0004
Revises: 0003
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, aliases, serving_label, serving_grams, kcal, protein_dg, carbs_dg, fat_dg, indian)
FOODS = [
    # ── Breads and staples ──────────────────────────────────────────────────
    ("roti", ["chapati", "phulka", "rotis", "chapatis"], "1 medium", 40, 71, 30, 150, 4, True),
    ("paratha", ["parathas"], "1 medium", 80, 210, 45, 270, 80, True),
    ("naan", ["naans"], "1 piece", 90, 260, 80, 450, 50, True),
    ("rice", ["chawal", "steamed rice", "white rice"], "1 katori", 150, 200, 40, 450, 4, True),
    ("brown rice", [], "1 katori", 150, 190, 45, 400, 15, True),
    ("poha", [], "1 katori", 150, 180, 30, 350, 50, True),
    ("upma", [], "1 katori", 150, 200, 40, 320, 70, True),
    ("idli", ["idlis"], "1 piece", 50, 58, 20, 120, 4, True),
    ("dosa", ["dosas", "plain dosa"], "1 piece", 100, 133, 27, 250, 37, True),
    ("bread slice", ["toast", "bread"], "1 slice", 30, 80, 30, 140, 10, False),

    # ── Dals and legumes ────────────────────────────────────────────────────
    ("dal", ["daal", "lentils", "toor dal", "moong dal"], "1 katori", 150, 120, 70, 200, 5, True),
    ("rajma", [], "1 katori", 150, 140, 80, 200, 15, True),
    ("chole", ["chana", "chickpea curry"], "1 katori", 150, 160, 80, 220, 40, True),
    ("sambar", [], "1 katori", 150, 100, 50, 150, 20, True),

    # ── Protein ─────────────────────────────────────────────────────────────
    ("egg", ["eggs", "boiled egg", "anda"], "1 large", 50, 78, 63, 6, 53, False),
    ("egg white", ["egg whites"], "1 large", 33, 17, 36, 2, 1, False),
    ("omelette", ["omelet"], "2 eggs", 120, 200, 130, 20, 150, False),
    ("paneer", ["cottage cheese"], "100 g", 100, 265, 180, 12, 210, True),
    ("chicken breast", ["chicken"], "100 g", 100, 165, 310, 0, 36, False),
    ("chicken curry", [], "1 katori", 150, 240, 200, 60, 140, True),
    ("fish", ["fish curry"], "100 g", 100, 180, 220, 20, 90, False),
    ("mutton", ["lamb"], "100 g", 100, 250, 250, 0, 160, False),
    ("whey protein", ["protein shake", "whey", "protein powder"], "1 scoop", 30,
     120, 240, 30, 15, False),
    ("soya chunks", ["soya"], "50 g dry", 50, 170, 260, 130, 5, True),

    # ── Dairy ───────────────────────────────────────────────────────────────
    ("curd", ["dahi", "yogurt", "yoghurt"], "1 katori", 150, 90, 50, 70, 50, True),
    ("milk", ["doodh"], "1 glass", 200, 120, 64, 96, 64, False),
    ("buttermilk", ["chaas", "chhach"], "1 glass", 200, 40, 20, 40, 10, True),
    ("ghee", [], "1 tsp", 5, 45, 0, 0, 50, True),

    # ── Vegetables and sides ────────────────────────────────────────────────
    ("mixed vegetable sabzi", ["sabzi", "sabji", "vegetables"], "1 katori", 150,
     110, 30, 130, 50, True),
    ("aloo sabzi", ["potato sabzi"], "1 katori", 150, 150, 30, 250, 40, True),
    ("palak paneer", [], "1 katori", 150, 220, 110, 90, 160, True),
    ("salad", ["green salad"], "1 bowl", 100, 40, 15, 70, 3, False),

    # ── Snacks and drinks ───────────────────────────────────────────────────
    ("banana", ["kela"], "1 medium", 118, 105, 13, 270, 4, False),
    ("apple", [], "1 medium", 180, 95, 5, 250, 3, False),
    ("almonds", ["badam"], "10 pieces", 12, 70, 26, 25, 60, False),
    ("peanuts", ["moongphali"], "30 g", 30, 170, 75, 50, 140, False),
    ("tea", ["chai", "milk tea"], "1 cup", 150, 60, 20, 80, 20, True),
    ("black coffee", [], "1 cup", 150, 5, 1, 5, 1, False),
    ("coffee with milk", ["coffee", "latte"], "1 cup", 150, 70, 30, 80, 30, False),
    ("samosa", ["samosas"], "1 piece", 60, 260, 40, 300, 130, True),
    ("biscuit", ["biscuits", "cookie"], "2 pieces", 20, 90, 12, 130, 35, False),
]


def upgrade() -> None:
    op.bulk_insert(
        sa.table(
            "foods",
            sa.column("name", sa.String),
            sa.column("aliases", sa.Text),
            sa.column("serving_label", sa.String),
            sa.column("serving_grams", sa.Integer),
            sa.column("kcal", sa.Integer),
            sa.column("protein_dg", sa.Integer),
            sa.column("carbs_dg", sa.Integer),
            sa.column("fat_dg", sa.Integer),
            sa.column("is_indian", sa.Boolean),
        ),
        [
            {
                "name": name,
                # Text rather than JSON so `alembic upgrade --sql` can render it;
                # Postgres casts on insert into the JSONB column.
                "aliases": json.dumps(aliases),
                "serving_label": serving_label,
                "serving_grams": grams,
                "kcal": kcal,
                "protein_dg": protein,
                "carbs_dg": carbs,
                "fat_dg": fat,
                "is_indian": indian,
            }
            for name, aliases, serving_label, grams, kcal, protein, carbs, fat, indian in FOODS
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM foods")
