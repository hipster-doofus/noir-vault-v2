-- Noir Vault v2 – Supabase Schema
-- Run this once in your Supabase SQL Editor

create table if not exists movies (
  id           uuid primary key default gen_random_uuid(),
  title        text not null,
  year         integer,
  director     text,
  source_url   text,
  archive_id   text,
  genre        text,
  search_terms text,
  is_public_domain boolean default true,
  created_at   timestamptz default now()
);

create table if not exists scenes (
  id             uuid primary key default gen_random_uuid(),
  movie_id       uuid references movies(id) on delete cascade,
  scene_number   integer not null,
  start_time     real,
  end_time       real,
  duration       real,
  transcript     text,
  description    text,
  synopsis       text,
  mood           text,
  noir_tropes    text[],
  thumbnail_url  text,
  download_url   text,
  release_tag    text,
  created_at     timestamptz default now()
);

-- Enable Row Level Security
alter table movies enable row level security;
alter table scenes enable row level security;

-- Allow anonymous reads (for the web gallery)
create policy "Public read movies"  on movies for select using (true);
create policy "Public read scenes"  on scenes for select using (true);

-- Allow service role to insert/update (used by the pipeline)
create policy "Service insert movies" on movies for insert with check (true);
create policy "Service insert scenes" on scenes for insert with check (true);
create policy "Service update movies" on movies for update using (true);
create policy "Service update scenes" on scenes for update using (true);
