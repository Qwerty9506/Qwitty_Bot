-- Выполни этот SQL в Supabase -> SQL Editor.
-- После этого бот сможет работать через SUPABASE_SERVICE_ROLE_KEY.

create table if not exists public.groups (
    group_id bigint primary key,
    title text not null,
    owner_id bigint not null,
    req_invites integer not null default 0 check (req_invites >= 0),
    spam_protect boolean not null default false
);

create table if not exists public.users (
    user_id bigint not null,
    group_id bigint not null,
    invites_count integer not null default 0 check (invites_count >= 0),
    is_allowed boolean not null default false,
    primary key (user_id, group_id)
);

create table if not exists public.group_users (
    group_id bigint not null,
    user_id bigint not null,
    first_name text not null default '',
    username text,
    primary key (group_id, user_id)
);

create table if not exists public.moderators (
    group_id bigint not null,
    user_id bigint not null,
    can_ban boolean not null default true,
    can_mute boolean not null default true,
    can_kick boolean not null default true,
    primary key (group_id, user_id)
);

create index if not exists idx_groups_owner_id
    on public.groups(owner_id);

create index if not exists idx_users_group_id
    on public.users(group_id);

create index if not exists idx_group_users_username
    on public.group_users(group_id, username);

create index if not exists idx_moderators_group_id
    on public.moderators(group_id);

-- Если в Supabase включена RLS, service_role обычно обходит её.
-- Эти таблицы используются только сервером бота, поэтому НЕ помещай
-- SUPABASE_SERVICE_ROLE_KEY в клиентский код, GitHub и публичные файлы.
