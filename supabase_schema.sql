-- 코인노래방 방 현황 앱: Supabase SQL Editor에서 그대로 실행하세요.
-- (기존 laundry-app 프로젝트와 같은 Supabase 프로젝트를 재사용해도, 새 프로젝트를 만들어도 안전하게 동작합니다.)

create table if not exists karaoke_rooms (
    id bigint generated always as identity primary key,
    room_number text not null unique,
    is_running boolean not null default false,
    end_time timestamptz,
    reported_minutes int,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists karaoke_analytics_events (
    id bigint generated always as identity primary key,
    event_type text not null,
    room_number text,
    created_at timestamptz not null default now()
);

-- 이 앱은 별도 로그인 없이 anon key로 직접 읽고 쓰므로 RLS를 끕니다.
-- (laundry-app과 동일한 신뢰 모델입니다.)
alter table karaoke_rooms disable row level security;
alter table karaoke_analytics_events disable row level security;
