""" fake data: what realistic values look like, as SQL expressions for each row g """

# types that only need a random value of their kind
# (text gets cut to fit varchar(n) by the final cast, dates are within the last 2 years)
TYPE_VALUES = {
    "text": "md5(random()::text)",
    "character varying": "md5(random()::text)",
    "character": "md5(random()::text)",
    "boolean": "random() < 0.5",
    "timestamp with time zone": "now() - random() * interval '730 days'",
    "timestamp without time zone": "now() - random() * interval '730 days'",
    "date": "current_date - floor(random() * 730)::int",
    "time with time zone": "time '00:00' + random() * interval '24 hours'",
    "time without time zone": "time '00:00' + random() * interval '24 hours'",
    "interval": "random() * interval '30 days'",
    "uuid": "gen_random_uuid()",
    "json": "json_build_object('n', g)",
    "jsonb": "json_build_object('n', g)",
}

# realistic values for common column names (unique columns keep their own rule)
FIRST_NAME = "(ARRAY['Ana', 'Ben', 'Chloe', 'David', 'Emma', 'Hugo', 'Ines', 'Liam', 'Maria', 'Noah'])[1 + floor(random() * 10)::int]"
LAST_NAME = "(ARRAY['Silva', 'Smith', 'Garcia', 'Muller', 'Rossi', 'Kim', 'Costa', 'Brown', 'Martin', 'Lee'])[1 + floor(random() * 10)::int]"
RECENT = "now() - random() ^ 2 * interval '730 days'"  # last 2 years, mostly recent
REVIEW = "(ARRAY['Great, would buy again.', 'Works fine.', 'Not what I expected.', 'Fast delivery.', 'Broke after a week.', 'Exactly as described.'])[1 + floor(random() * 6)::int]"
NAME_VALUES = {
    "first_name": FIRST_NAME,
    "last_name": LAST_NAME,
    "full_name": f"{FIRST_NAME} || ' ' || {LAST_NAME}",
    "email": "'user' || floor(random() * 1000000) || '@example.com'",
    "phone": "'+1 555 ' || lpad(floor(random() * 10000)::text, 4, '0')",
    # a few countries hold most of the rows
    "country": "(ARRAY['United States', 'United Kingdom', 'Germany', 'France', 'Portugal', 'Brazil', 'India', 'Japan'])[1 + floor(random() ^ 2 * 8)::int]",
    "is_active": "random() < 0.9",
    "created_at": RECENT,
    "updated_at": RECENT,
    "body": REVIEW,
    "comment": REVIEW,
    "description": REVIEW,
}

# usual ranges for common number columns, used where the CHECKs don't say
NUMBER_RANGES = {
    "quantity": (1, 5),
    "price": (1, 500),
    "unit_price": (1, 500),
    "amount": (1, 1000),
    "total": (5, 2000),
    "stock": (0, 200),
    "age": (18, 80),
}
