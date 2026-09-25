import io
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

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

# 코인노래방은 시간이 아니라 곡 수로 결제하는 경우가 많아, 곡 수를 분으로 환산해
# 기존 종료 시각 로직에 그대로 얹는다. 3.5분/곡은 평균치 추정값이라 버튼에 예상
# 분을 함께 보여줘 손님이 확인할 수 있게 한다.
MINUTES_PER_SONG = 3.5
SONG_PRESETS = [3, 6, 9, 12]

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
def room_sort_key(room_number):
    # room_number는 text 컬럼이라 DB에서 정렬하면 "1,10,11,2,20,3..." 처럼 문자열
    # 순서가 되어버린다. 숫자로만 된 방 번호는 숫자로, 그 외("VIP1" 등)는 뒤로
    # 보내 문자열로 정렬한다.
    try:
        return (0, int(room_number))
    except ValueError:
        return (1, room_number)


def load_rooms():
    try:
        res = request_with_retry(
            "GET", ROOMS_URL, headers=SUPABASE_HEADERS, params={"select": "*"}
        )
        if res.status_code == 200:
            return sorted(res.json(), key=lambda r: room_sort_key(r["room_number"]))
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


def room_link(room_number):
    """방 하나를 가리키는 딥링크(?room=...)를 만든다. QR코드와 그리드 타일이 같은 링크 규칙을 쓴다."""
    return f"?room={quote(str(room_number))}"


def start_checkin(room, minutes, mode):
    # charge_mode를 함께 저장해서, 다음에 추가할 때는 처음 고른 방식(시간/곡수)
    # 으로만 추가할 수 있도록 한다 - 매번 다시 고르게 하면 혼란스러워서.
    end_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    save_room(
        room["id"],
        {"is_running": True, "end_time": end_time.isoformat(), "reported_minutes": minutes, "charge_mode": mode},
    )
    log_event("check_in", room["room_number"])
    st.success(f"{room['room_number']}호 체크인 완료! 잠시 후 화면이 갱신됩니다.")
    time.sleep(1)
    st.rerun()


def extend_time(room, add_minutes, label):
    current_end = parse_end_time(room) or datetime.now(timezone.utc)
    save_room(room["id"], {"end_time": (current_end + timedelta(minutes=add_minutes)).isoformat()})
    log_event("extend_time", room["room_number"])
    st.success(f"{label} 추가 완료!")
    time.sleep(1)
    st.rerun()


# ── 3. UI 화면 렌더링 ─────────────────────────────────────────────
st.set_page_config(page_title="코인노래방 방 현황", layout="centered", page_icon="🎤")

# 20~30대가 선호할 만한 파스텔톤 카드 디자인. 방 카드는 key 접두사(room_available_/
# room_occupied_)로 상태를 구분해 CSS에서 각각 다른 색을 입힌다. 자동 새로고침으로
# 텍스트만 바뀌는 요소(지표, 카드 제목)는 transition을 꺼서 깜빡임을 막는다.
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Jua&family=Nunito:wght@400;600;700;800&display=swap');

    html, body, [data-testid="stAppViewContainer"] {
        font-family: 'Nunito', 'Noto Sans KR', sans-serif;
    }
    [data-testid="stAppViewContainer"] {
        background: linear-gradient(160deg, #FFE3F1 0%, #F2E4FF 45%, #E2EFFF 100%);
        background-attachment: fixed;
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stMainBlockContainer"] { max-width: 680px; padding-top: 2.5rem; }

    .cute-header {
        background: #FFFFFF;
        border-radius: 28px;
        padding: 22px 28px;
        box-shadow: 0 10px 30px rgba(168, 121, 217, 0.18);
        margin-bottom: 22px;
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .cute-header .emoji { font-size: 2.2rem; }
    .cute-header h1 { font-family: 'Jua', sans-serif; font-size: 1.6rem; color: #4A3F55; margin: 0; }
    .cute-header .sub { font-size: 0.85rem; color: #B79ACB; font-weight: 700; margin-top: 2px; }

    [data-testid="stHeading"] h2, [data-testid="stHeading"] h3,
    [data-testid="stMarkdownContainer"] h1, [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3, [data-testid="stMarkdownContainer"] h4 {
        font-family: 'Jua', sans-serif !important;
        color: #4A3F55 !important;
    }
    [data-testid="stCaptionContainer"] { color: #A88CC2 !important; font-weight: 700 !important; }

    /* 전체 현황 그리드 (방이 많아도 한눈에 보이도록) */
    .room-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(108px, 1fr));
        gap: 12px;
        margin-bottom: 8px;
    }
    .room-tile {
        position: relative;
        display: block;
        text-decoration: none !important;
        border-radius: 20px;
        padding: 14px 14px 28px;
        min-height: 70px;
        border: 2px solid transparent;
        box-shadow: 0 6px 16px rgba(168, 121, 217, 0.14);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .room-tile:hover { transform: translateY(-3px); box-shadow: 0 10px 22px rgba(168, 121, 217, 0.24); }
    .room-tile.available { background: #CFF9E1; border-color: #6EE7B7; }
    .room-tile.occupied { background: #FFCBDB; border-color: #FF8FAA; }
    .room-tile .tile-num { display: block; font-family: 'Jua', sans-serif; font-size: 1.15rem; color: #3F3350; }
    .room-tile .tile-status { display: block; font-weight: 800; font-size: 0.8rem; color: #5C4F6B; margin-top: 4px; }
    .room-tile .tile-tag {
        position: absolute; bottom: 8px; right: 12px;
        font-size: 0.62rem; font-weight: 800; letter-spacing: 0.03em; color: #5C4F6B; opacity: 0.6;
    }

    /* 방 상세 화면의 "다른 방 보기" 링크 */
    .back-link {
        display: inline-block;
        text-decoration: none !important;
        background: #FFFFFF;
        border: 2px solid #F0DFFF;
        color: #9B6FE3 !important;
        font-weight: 800;
        border-radius: 999px;
        padding: 10px 22px;
        margin-bottom: 18px;
        transition: transform 0.15s ease, background 0.15s ease;
    }
    .back-link:hover { background: #FBF3FF; transform: translateY(-1px); }

    div[class*="st-key-room_available_"],
    div[class*="st-key-room_occupied_"] {
        background: #FFFFFF !important;
        border: none !important;
        border-radius: 22px !important;
        padding: 18px 22px !important;
        box-shadow: 0 8px 22px rgba(168, 121, 217, 0.14);
        margin-bottom: 16px !important;
    }
    div[class*="st-key-room_available_"] { border-left: 8px solid #4ADE80 !important; }
    div[class*="st-key-room_occupied_"] { border-left: 8px solid #FF7A9C !important; }

    .room-card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; transition: none !important; }
    .room-num { font-family: 'Jua', sans-serif; font-size: 1.35rem; color: #4A3F55; }
    .status-pill { display: inline-block; padding: 6px 18px; border-radius: 999px; font-weight: 800; font-size: 0.85rem; border: 1.5px solid transparent; }
    .status-pill.available { background: #6EE7B7; color: #065F46; border-color: #34D399; }
    .status-pill.occupied { background: #FF9DB5; color: #881337; border-color: #FF6F91; }

    [data-testid="stMetric"] { background: #FFF7FB; border-radius: 16px; padding: 10px 16px; transition: none !important; }
    [data-testid="stMetricValue"] { font-family: 'Jua', sans-serif !important; color: #FF6FA5 !important; font-size: 1.5rem !important; transition: none !important; }
    [data-testid="stMetricLabel"] p { color: #B79ACB !important; font-weight: 700 !important; font-size: 0.78rem !important; }

    .stButton button, [data-testid^="stBaseButton"] {
        border-radius: 999px !important;
        font-weight: 800 !important;
        transition: transform 0.15s ease, box-shadow 0.15s ease !important;
    }
    [data-testid="stBaseButton-primaryFormSubmit"],
    [data-testid="stBaseButton-primary"] {
        background: linear-gradient(135deg, #FF6FA5, #C084FC) !important;
        border: none !important;
        color: #fff !important;
        box-shadow: 0 8px 18px rgba(255, 111, 165, 0.35) !important;
    }
    [data-testid="stBaseButton-primaryFormSubmit"]:hover,
    [data-testid="stBaseButton-primary"]:hover { transform: translateY(-2px); box-shadow: 0 10px 22px rgba(255, 111, 165, 0.45) !important; }
    [data-testid="stBaseButton-secondaryFormSubmit"],
    [data-testid="stBaseButton-secondary"] {
        background: #FFFFFF !important;
        border: 2px solid #F0DFFF !important;
        color: #9B6FE3 !important;
    }
    [data-testid="stBaseButton-secondaryFormSubmit"]:hover,
    [data-testid="stBaseButton-secondary"]:hover { background: #FBF3FF !important; transform: translateY(-1px); }

    [data-testid="stNumberInputContainer"],
    [data-testid="stTextInputRootElement"],
    [data-testid="stSelectbox"] div[role="group"] {
        border-radius: 16px !important;
        border: 2px solid #F1E0FF !important;
        background: #FFFDFF !important;
    }
    [data-testid="stSelectbox"] input,
    [data-testid="stNumberInputField"],
    [data-testid="stTextInputField"] { font-weight: 700 !important; color: #4A3F55 !important; }
    [data-testid="stWidgetLabel"] p { color: #8C76A6 !important; font-weight: 700 !important; font-size: 0.85rem !important; }

    [data-testid="stForm"] {
        background: #FFFFFF;
        border: none !important;
        border-radius: 24px;
        padding: 22px !important;
        box-shadow: 0 8px 22px rgba(168, 121, 217, 0.12);
    }
    [data-testid="stExpander"] {
        background: #FFFFFF;
        border-radius: 20px !important;
        border: 2px dashed #E9D3FF !important;
        overflow: hidden;
    }
    [data-testid="stAlertContainer"] { border-radius: 18px !important; border: none !important; font-weight: 600; }
    [data-testid="stMarkdownContainer"] hr { border-color: #F1DFFF !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "visit_logged" not in st.session_state:
    log_event("page_view")
    st.session_state["visit_logged"] = True

rooms = load_rooms()
now = datetime.now(timezone.utc)
room_numbers = [r["room_number"] for r in rooms]
selected_room = next((r for r in rooms if r["room_number"] == st.query_params.get("room", "")), None)


def render_admin():
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
                        save_room(
                            room["id"],
                            {"is_running": False, "end_time": None, "reported_minutes": None, "charge_mode": None},
                        )
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
                    checkin_url = f"{APP_BASE_URL}/{room_link(qr_room)}"
                    st.image(make_qr_image(checkin_url), caption=checkin_url, width=200)
        elif admin_pw:
            st.error("비밀번호가 일치하지 않습니다.")


def render_overview():
    st.markdown(
        """
        <div class="cute-header">
            <span class="emoji">🎤</span>
            <div>
                <h1>코인노래방 방 현황</h1>
                <div class="sub">방을 눌러서 체크인하세요</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not rooms:
        st.info("아직 등록된 방이 없습니다. 아래 관리자 메뉴에서 방을 추가해주세요.")
    else:
        empty_count = sum(1 for r in rooms if not ((remaining_seconds(r, now) or 0) > 0))
        st.caption(f"현재 비어있는 방: {empty_count} / {len(rooms)}개")

        tiles = []
        for room in rooms:
            secs = remaining_seconds(room, now)
            link = room_link(room["room_number"])
            if secs and secs > 0:
                mins = int(secs // 60)
                tiles.append(
                    f'<a href="{link}" target="_self" class="room-tile occupied">'
                    f'<span class="tile-num">{room["room_number"]}호</span>'
                    f'<span class="tile-status">{mins}분 남음</span>'
                    f'<span class="tile-tag">IN USE</span></a>'
                )
            else:
                tiles.append(
                    f'<a href="{link}" target="_self" class="room-tile available">'
                    f'<span class="tile-num">{room["room_number"]}호</span>'
                    f'<span class="tile-status">사용 가능</span>'
                    f'<span class="tile-tag">EMPTY</span></a>'
                )
        st.markdown(f'<div class="room-grid">{"".join(tiles)}</div>', unsafe_allow_html=True)

    st.divider()
    render_admin()
    st.divider()
    st.caption("🛠️ 오류가 발생하거나 앱이 작동하지 않을 때는 운영자에게 문의해주세요.")


def render_detail(room):
    st.markdown('<a href="?" target="_self" class="back-link">← 다른 방 보기</a>', unsafe_allow_html=True)

    st.markdown(
        f"""
        <div class="cute-header">
            <span class="emoji">🎤</span>
            <div>
                <h1>{room['room_number']}호</h1>
                <div class="sub">체크인 / 시간 갱신</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    secs = remaining_seconds(room, now)
    occupied = secs is not None and secs > 0
    card_key = f"room_occupied_{room['id']}" if occupied else f"room_available_{room['id']}"

    with st.container(border=True, key=card_key):
        if occupied:
            mins, s = int(secs // 60), int(secs % 60)
            end_local = parse_end_time(room).astimezone(KST)
            st.markdown(
                '<div class="room-card-head"><span class="status-pill occupied">사용중 🎤</span></div>',
                unsafe_allow_html=True,
            )
            col1, col2 = st.columns(2)
            col1.metric("남은 시간", f"{mins}분 {s}초")
            col2.metric("종료 예정", end_local.strftime("%H:%M"))
        else:
            st.markdown(
                '<div class="room-card-head"><span class="status-pill available">사용 가능 ✨</span></div>',
                unsafe_allow_html=True,
            )
            col1, col2 = st.columns(2)
            col1.metric("남은 시간", "-")
            col2.metric("종료 예정", "-")

    if occupied:
        # 이미 시작할 때 고른 방식(시간/곡수)으로만 추가할 수 있다 - 기존 데이터에는
        # charge_mode가 없을 수 있어 기본값은 "시간"으로 둔다.
        mode = room.get("charge_mode") or "time"

        if mode == "songs":
            st.markdown("#### 🎶 추가 시간 갱신 (곡 수)")
            st.caption("추가로 결제한 곡 수를 고르면 남은 시간에 더해져요.")

            song_cols = st.columns(len(SONG_PRESETS))
            for col, songs in zip(song_cols, SONG_PRESETS):
                add_minutes = round(songs * MINUTES_PER_SONG)
                clicked = col.button(
                    f"{songs}곡",
                    key=f"add_{songs}_{room['id']}",
                    type="primary",
                    use_container_width=True,
                    help=f"약 {add_minutes}분 추가돼요",
                )
                if clicked:
                    extend_time(room, add_minutes, f"{songs}곡(약 {add_minutes}분)")

            show_custom = st.toggle("그 외 (직접 곡 수 입력)", key=f"custom_toggle_{room['id']}")
            if show_custom:
                custom_songs = st.number_input(
                    "추가할 곡 수", min_value=1, max_value=50, value=1, step=1, key=f"custom_songs_{room['id']}"
                )
                if st.button("추가하기", key=f"custom_add_{room['id']}", type="primary", use_container_width=True):
                    add_minutes = round(custom_songs * MINUTES_PER_SONG)
                    extend_time(room, add_minutes, f"{custom_songs}곡(약 {add_minutes}분)")
        else:
            st.markdown("#### ⏱️ 추가 시간 갱신 (시간)")
            with st.form(f"extend_time_form_{room['id']}"):
                add_minutes = st.number_input("추가할 시간(분)", min_value=1, max_value=180, value=10, step=5)
                submitted = st.form_submit_button("추가하기", type="primary", use_container_width=True)
                if submitted:
                    extend_time(room, int(add_minutes), f"{int(add_minutes)}분")

        st.divider()
        st.caption("아직 시간이 남았지만 먼저 나가시나요? 누르면 이 방이 바로 빈 방으로 표시돼요.")
        if st.button("🚪 중단하고 나가기", key=f"early_exit_{room['id']}", use_container_width=True):
            save_room(
                room["id"],
                {"is_running": False, "end_time": None, "reported_minutes": None, "charge_mode": None},
            )
            log_event("early_exit", room["room_number"])
            st.success(f"{room['room_number']}호, 이용해주셔서 감사합니다! 방을 비웠어요.")
            time.sleep(1)
            st.rerun()
    else:
        st.markdown("#### 📱 체크인 방식 선택")
        mode_label = st.segmented_control(
            "무엇을 기준으로 결제하셨나요?",
            ["⏱️ 시간으로", "🎵 곡 수로"],
            default="⏱️ 시간으로",
            key=f"start_mode_{room['id']}",
        )

        if mode_label == "🎵 곡 수로":
            st.caption("결제하신 곡 수를 골라주세요. 이후 추가도 곡 수로만 가능해요.")
            song_cols = st.columns(len(SONG_PRESETS))
            for col, songs in zip(song_cols, SONG_PRESETS):
                add_minutes = round(songs * MINUTES_PER_SONG)
                clicked = col.button(
                    f"{songs}곡",
                    key=f"start_song_{songs}_{room['id']}",
                    type="primary",
                    use_container_width=True,
                    help=f"약 {add_minutes}분",
                )
                if clicked:
                    start_checkin(room, add_minutes, "songs")

            show_custom = st.toggle("그 외 (직접 곡 수 입력)", key=f"start_custom_toggle_{room['id']}")
            if show_custom:
                custom_songs = st.number_input(
                    "곡 수", min_value=1, max_value=50, value=1, step=1, key=f"start_custom_songs_{room['id']}"
                )
                if st.button("체크인", key=f"start_custom_btn_{room['id']}", type="primary", use_container_width=True):
                    add_minutes = round(custom_songs * MINUTES_PER_SONG)
                    start_checkin(room, add_minutes, "songs")
        else:
            st.caption("기기 화면에 표시된 '남은 시간'을 그대로 입력해주세요. 이후 추가도 시간으로만 가능해요.")
            with st.form(f"checkin_form_time_{room['id']}"):
                minutes = st.number_input("기기에 표시된 남은 시간(분)", min_value=1, max_value=300, value=30, step=5)
                submitted = st.form_submit_button("체크인 / 시간 갱신 🎤", type="primary", use_container_width=True)
                if submitted:
                    start_checkin(room, int(minutes), "time")

    st.divider()
    st.caption("🛠️ 오류가 발생하거나 앱이 작동하지 않을 때는 운영자에게 문의해주세요.")


if selected_room:
    render_detail(selected_room)
else:
    render_overview()

# ── 4. 자동 새로고침 ────────────────────────────────────────────
time.sleep(10)
st.rerun()
