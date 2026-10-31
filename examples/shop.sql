-- Example schema for a small online shop.

CREATE TABLE users (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    country     text NOT NULL,
    plan        text NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'enterprise')),
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE categories (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    parent_id  integer REFERENCES categories (id)
);

CREATE TABLE products (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_id  integer NOT NULL REFERENCES categories (id),
    sku          varchar(32) NOT NULL UNIQUE,
    name         text NOT NULL,
    price        numeric(10, 2) NOT NULL,
    stock        integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE order_status AS ENUM ('delivered', 'shipped', 'paid', 'pending', 'cancelled');

CREATE TABLE orders (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users (id),
    status      order_status NOT NULL DEFAULT 'pending',
    total       numeric(12, 2) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    shipped_at  timestamptz
);

CREATE TABLE order_items (
    order_id    bigint NOT NULL REFERENCES orders (id),
    product_id  bigint NOT NULL REFERENCES products (id),
    quantity    integer NOT NULL CHECK (quantity > 0),
    unit_price  numeric(10, 2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE reviews (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  bigint NOT NULL REFERENCES products (id),
    user_id     bigint NOT NULL REFERENCES users (id),
    rating      smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX products_category_idx ON products (category_id);