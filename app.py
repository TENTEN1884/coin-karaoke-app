import io
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import qrcode
import requests
import streamlit as st

# ── 1. Streamlit 비밀 금고(Secrets)에서 정보 가져오기 ──────────────
RAW_SUPABASE_URL = st.secrets["SUPABASE_URL"]
RAW_SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "")
APP_BASE_URL = st.secrets.get("APP_BASE_URL", "").strip().rstrip("/")

_parsed = urlparse(RAW_SUPABASE_URL.strip())
if _parsed.scheme and _parsed.netloc:
    SUPABASE_URL = f"{_parsed.scheme}://{_parsed.netloc}"
else:
    SUPABASE_URL = RAW_SUPABASE_URL.strip().rstrip("/")

SUPABASE_KEY = RAW_SUPABASE_KEY.strip()
ROOMS_URL = f"{SUPABASE_URL}/rest/v1/karaoke_rooms"
EVENTS_URL = f"{SUPABASE_URL}/rest/v1/karaoke_analytics_events"

# 한국 표준시 (UTC+9) - 종료 예정 시각을 로컬 시간으로 보여주기 위함
KST = timezone(timedelta(hours=9))

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


# 배포 환경에서는 첫 요청이 느릴 때가 있어(콜드 스타트 등), 타임아웃을 넉넉히 주고
# 실패 시 한 번 더 재시도해서 일시적인 "연결 오류"를 줄인다.
def request_with_retry(method, url, retries=2, **kwargs):
    kwargs.setdefault("timeout", 10)
    last_error = None
    for attempt in range(retries):
        try:
            return requests.request(method, url, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(1)
    raise last_error


# ── 2. Supabase 방 상태 관리 함수 ──────────────────────────────────
def load_rooms():
    try:
        res = request_with_retry(
            "GET",
            ROOMS_URL,
            headers=SUPABASE_HEADERS,
            params={"select": "*", "order": "room_number.asc"},
        )
        if res.status_code == 200:
            return res.json()
        st.error(f"Supabase 오류: {res.text}")
    except Exception as e:
        st.error(f"연결 오류: {e}")
    return []


def save_room(room_id, fields):
    payload = dict(fields)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        request_with_retry(
            "PATCH",
            ROOMS_URL,
            headers=SUPABASE_HEADERS,
            params={"id": f"eq.{room_id}"},
            json=payload,
        )
    except Exception as e:
        st.error(f"저장 오류: {e}")


def create_room(room_number):
    try:
        res = request_with_retry(
            "POST",
            ROOMS_URL,
            headers=SUPABASE_HEADERS,
            json={"room_number": room_number, "is_running": False},
        )
        if res.status_code not in (200, 201):
            st.error(f"방 추가 실패: {res.text}")
            return False
        return True
    except Exception as e:
        st.error(f"방 추가 오류: {e}")
        return False


def delete_room(room_id):
    try:
        request_with_retry(
            "DELETE", ROOMS_URL, headers=SUPABASE_HEADERS, params={"id": f"eq.{room_id}"}
        )
    except Exception as e:
        st.error(f"방 삭제 오류: {e}")


def log_event(event_type, room_number=None):
    try:
        requests.post(
            EVENTS_URL,
            headers=SUPABASE_HEADERS,
            json={"event_type": event_type, "room_number": room_number},
            timeout=5,
        )
    except Exception:
        pass


def parse_end_time(room):
    if not room.get("end_time"):
        return None
    end_time = datetime.fromisoformat(room["end_time"])
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    return end_time


def remaining_seconds(room, now):
    if not room.get("is_running"):
        return None
    end_time = parse_end_time(room)
    if end_time is None:
        return None
    return (end_time - now).total_seconds()


def make_qr_image(data):
    qr = qrcode.make(data)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ── 3. UI 화면 렌더링 ─────────────────────────────────────────────
st.set_page_config(page_title="코인노래방 방 현황", layout="centered", page_icon="🎤")

# 화면이 자동으로 자주 새로고침되면서 이전 화면 요소가 옅게 남았다가 사라지는
# 전환 애니메이션(잔상 현상)이 보일 수 있어, 관련 트랜지션/애니메이션을 꺼서
# 화면이 바로바로 전환되도록 한다.
st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] * {
        transition: none !important;
        animation: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if "visit_logged" not in st.session_state:
    log_event("page_view")
    st.session_state["visit_logged"] = True

rooms = load_rooms()
now = datetime.now(timezone.utc)

st.title("🎤 코인노래방 방 현황")

if not rooms:
    st.info("아직 등록된 방이 없습니다. 아래 관리자 메뉴에서 방을 추가해주세요.")
else:
    empty_rooms = [r for r in rooms if remaining_seconds(r, now) is None or remaining_seconds(r, now) <= 0]
    running_rooms = sorted(
        [r for r in rooms if (remaining_seconds(r, now) or 0) > 0],
        key=lambda r: remaining_seconds(r, now),
    )

    st.caption(f"현재 비어있는 방: {len(empty_rooms)} / {len(rooms)}개")

    # 방마다 카드 내부 구성(제목 + 2열 지표)을 상태와 무관하게 항상 동일하게 유지한다.
    # 재실행마다 카드 안의 위젯 개수가 늘었다 줄었다 하면 Streamlit이 이전 값을 다 지우지
    # 못하고 옅게 남기는 경우가 있어서, 비어있을 때도 지표 자리를 "-"로 채워둔다.
    for room in empty_rooms:
        with st.container(border=True, key=f"room_card_{room['id']}"):
            st.markdown(f"### 🟢 {room['room_number']}호 — 사용 가능")
            col1, col2 = st.columns(2)
            col1.metric("남은 시간", "-")
            col2.metric("종료 예정", "-")

    for room in running_rooms:
        secs = remaining_seconds(room, now)
        mins, s = int(secs // 60), int(secs % 60)
        end_local = parse_end_time(room).astimezone(KST)
        with st.container(border=True, key=f"room_card_{room['id']}"):
            st.markdown(f"### 🔴 {room['room_number']}호")
            col1, col2 = st.columns(2)
            col1.metric("남은 시간", f"{mins}분 {s}초")
            col2.metric("종료 예정", end_local.strftime("%H:%M"))

st.divider()

# ── 4. 손님 체크인 (QR로 들어오면 방이 자동 선택됨) ─────────────────
st.subheader("📱 체크인 / 시간 갱신")
st.caption("방에 들어가면 기기 화면에 표시된 '남은 시간'을 그대로 입력해주세요. 추가 결제로 시간이 늘어났을 때도 같은 방법으로 다시 입력하면 갱신됩니다.")

room_numbers = [r["room_number"] for r in rooms]
query_room = st.query_params.get("room", "")
default_index = room_numbers.index(query_room) if query_room in room_numbers else 0

if room_numbers:
    with st.form("checkin_form"):
        selected_room = st.selectbox("방 번호", room_numbers, index=default_index)
        minutes = st.number_input("기기에 표시된 남은 시간(분)", min_value=1, max_value=300, value=30, step=5)
        submitted = st.form_submit_button("체크인 / 시간 갱신 🎤", type="primary", use_container_width=True)

        if submitted:
            target = next(r for r in rooms if r["room_number"] == selected_room)
            end_time = datetime.now(timezone.utc) + timedelta(minutes=int(minutes))
            save_room(
                target["id"],
                {"is_running": True, "end_time": end_time.isoformat(), "reported_minutes": int(minutes)},
            )
            log_event("check_in", selected_room)
            st.success(f"{selected_room}호 체크인 완료! 잠시 후 화면이 갱신됩니다.")
            time.sleep(1)
            st.rerun()
else:
    st.info("등록된 방이 없어 체크인할 수 없습니다.")

st.divider()

# ── 5. 관리자 (방 등록/삭제, 강제 초기화, 체크인 QR코드) ────────────
with st.expander("⚙️ 관리자"):
    admin_pw = st.text_input("관리자 비밀번호", type="password", key="admin_pw")

    if admin_pw and ADMIN_PASSWORD and admin_pw == ADMIN_PASSWORD:
        st.success("관리자 인증됨")

        st.markdown("#### 방 추가")
        with st.form("add_room_form"):
            new_room = st.text_input("새 방 번호 (예: 1, VIP1)")
            add_submitted = st.form_submit_button("방 추가")
            if add_submitted:
                if new_room.strip():
                    if create_room(new_room.strip()):
                        log_event("admin_add_room", new_room.strip())
                        st.rerun()
                else:
                    st.error("방 번호를 입력해주세요.")

        if rooms:
            st.markdown("#### 방별 관리")
            for room in rooms:
                cols = st.columns([2, 1, 1])
                cols[0].write(f"**{room['room_number']}호**")
                if cols[1].button("초기화", key=f"reset_{room['id']}"):
                    save_room(room["id"], {"is_running": False, "end_time": None, "reported_minutes": None})
                    log_event("admin_reset_room", room["room_number"])
                    st.rerun()
                if cols[2].button("삭제", key=f"delete_{room['id']}"):
                    delete_room(room["id"])
                    log_event("admin_delete_room", room["room_number"])
                    st.rerun()

            st.markdown("#### 체크인 QR코드")
            if not APP_BASE_URL:
                st.warning("secrets.toml에 APP_BASE_URL(배포된 앱 주소)을 설정하면 방마다 체크인 QR코드를 보여드려요.")
            else:
                qr_room = st.selectbox("QR을 확인할 방", room_numbers, key="qr_room")
                checkin_url = f"{APP_BASE_URL}/?room={qr_room}"
                st.image(make_qr_image(checkin_url), caption=checkin_url, width=200)
    elif admin_pw:
        st.error("비밀번호가 일치하지 않습니다.")

st.divider()
st.caption("🛠️ 오류가 발생하거나 앱이 작동하지 않을 때는 운영자에게 문의해주세요.")

# ── 6. 자동 새로고침 ────────────────────────────────────────────
time.sleep(10)
st.rerun()
