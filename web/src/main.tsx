import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Button } from "@heroui/react";
import type HlsType from "hls.js";
import {
  AnimatePresence,
  MotionConfig,
  motion,
  useDragControls,
  useMotionValue,
  useReducedMotion,
} from "framer-motion";
import { BrowserRouter, useLocation, useNavigate } from "react-router-dom";
import {
  Archive,
  AudioLines,
  CircleDot,
  Gauge,
  HardDrive,
  Headphones,
  LayoutDashboard,
  LogOut,
  Plus,
  Play,
  Pause,
  Radio,
  Settings,
  Shield,
  Trash2,
  Users,
  Video,
  Volume2,
  VolumeX,
  Maximize,
  Maximize2,
  PictureInPicture2,
} from "lucide-react";
import "./styles.css";
import "./player.css";
import "./motion.css";

type User = {
  id: number;
  username: string;
  role: "admin" | "user";
  audio_format: "aac" | "flac";
  cookie_status: string;
};
type Recording = {
  id: number;
  state: string;
  type: "live" | "vod" | "clip";
  title: string;
  channel: string;
  channel_id: string;
  thumbnail?: string;
  size: number;
  total_size: number;
  progress?: number;
  speed_bps: number;
  eta_seconds?: number;
  encoding_progress?: number;
  encoding_speed?: number;
  encoding_eta_seconds?: number;
  encoding_processed_seconds?: number;
  encoding_state?: string;
  recorded_seconds: number;
  duration_seconds: number;
  recording_active: boolean;
  created_at: string;
  error?: string;
};

const IN_FLIGHT_STATES = new Set(["queued", "recording", "processing"]);
const MOTION_EASE = [0.22, 1, 0.36, 1] as const;

const motionDuration = (reduceMotion: boolean | null, duration = 0.22) =>
  reduceMotion ? 0.001 : duration;

type ConfirmOptions = {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
};

type ConfirmRequest = ConfirmOptions & {
  resolve: (confirmed: boolean) => void;
};

const ConfirmContext = createContext<
  ((options: ConfirmOptions) => Promise<boolean>) | null
>(null);

function useConfirm() {
  const confirmAction = useContext(ConfirmContext);
  if (!confirmAction) throw new Error("ConfirmProvider가 필요합니다");
  return confirmAction;
}

function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const previousRequest = useRef<ConfirmRequest | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const reduceMotion = useReducedMotion();

  const confirmAction = useCallback((options: ConfirmOptions) => {
    previousRequest.current?.resolve(false);
    const activeElement = document.activeElement as HTMLElement | null;
    if (!activeElement?.closest(".confirm-dialog"))
      opener.current = activeElement;
    return new Promise<boolean>((resolve) => {
      const next = { ...options, resolve };
      previousRequest.current = next;
      setRequest(next);
    });
  }, []);

  const settle = useCallback((confirmed: boolean) => {
    const current = previousRequest.current;
    if (!current) return;
    previousRequest.current = null;
    setRequest(null);
    current.resolve(confirmed);
  }, []);

  useEffect(() => {
    if (!request) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    cancelButton.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        settle(false);
        return;
      }
      if (event.key !== "Tab" || !dialog.current) return;
      const buttons = Array.from(
        dialog.current.querySelectorAll<HTMLButtonElement>(
          "button:not([disabled])",
        ),
      );
      if (!buttons.length) return;
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [request, settle]);

  return (
    <ConfirmContext.Provider value={confirmAction}>
      {children}
      <AnimatePresence
        onExitComplete={() => {
          opener.current?.focus();
          opener.current = null;
        }}
      >
        {request && (
          <motion.div className="confirm-overlay" key="confirm-dialog">
            <motion.div
              className="confirm-backdrop"
              aria-hidden="true"
              onClick={() => settle(false)}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{
                duration: motionDuration(reduceMotion, 0.18),
                ease: MOTION_EASE,
              }}
            />
            <motion.section
              ref={dialog}
              className={`confirm-dialog ${request.tone === "danger" ? "danger" : ""}`}
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="confirm-dialog-title"
              aria-describedby={
                request.description ? "confirm-dialog-description" : undefined
              }
              initial={reduceMotion ? false : { opacity: 0, scale: 0.97, y: 8 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={
                reduceMotion
                  ? { opacity: 0 }
                  : { opacity: 0, scale: 0.98, y: 4 }
              }
              transition={{
                duration: motionDuration(reduceMotion, 0.22),
                ease: MOTION_EASE,
              }}
            >
              <small>
                {request.tone === "danger"
                  ? "DESTRUCTIVE ACTION"
                  : "CONFIRM ACTION"}
              </small>
              <h2 id="confirm-dialog-title">{request.title}</h2>
              {request.description && (
                <p id="confirm-dialog-description">{request.description}</p>
              )}
              <div className="confirm-actions">
                <button
                  ref={cancelButton}
                  type="button"
                  className="confirm-cancel"
                  onClick={() => settle(false)}
                >
                  {request.cancelLabel || "취소"}
                </button>
                <button
                  type="button"
                  className="confirm-submit"
                  onClick={() => settle(true)}
                >
                  {request.confirmLabel || "확인"}
                </button>
              </div>
            </motion.section>
          </motion.div>
        )}
      </AnimatePresence>
    </ConfirmContext.Provider>
  );
}

/** Whether a query payload still contains work that needs live updates. */
function hasActiveWork(data: unknown): boolean {
  if (!Array.isArray(data)) return false;
  return data.some(
    (item) =>
      item &&
      typeof item === "object" &&
      (IN_FLIGHT_STATES.has((item as Recording).state) ||
        (item as Subscription).live === true),
  );
}
type Subscription = {
  id: number;
  channel_id: string;
  name: string;
  image?: string;
  live: boolean;
  auto_record: boolean;
};
type SubscribeResult = {
  id: number;
  channel_id: string;
  name: string;
  image?: string;
  auto_record: boolean;
  live: boolean;
  live_title?: string | null;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok)
    throw new Error(
      (await response.json().catch(() => ({}))).detail || "요청에 실패했습니다",
    );
  return response.status === 204 ? (undefined as T) : response.json();
}
const size = (n: number) => {
  if (!n) return "—";
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024).toFixed(1)} KB`;
};
const typeLabel = (type: Recording["type"]) =>
  type === "vod" ? "VIDEO" : type.toUpperCase();
const stateLabel = (r: Recording) =>
  r.state === "processing"
    ? "ENCODING"
    : r.state === "recording" || r.state === "queued"
      ? r.type === "live"
        ? "RECORDING"
        : "DOWNLOADING"
      : r.state === "completed"
        ? r.type === "live"
          ? "RECORDED"
          : "DOWNLOADED"
        : r.state.toUpperCase();
const eta = (seconds?: number) => {
  if (seconds == null) return "남은 시간 계산 중";
  if (seconds < 60) return `약 ${Math.max(1, Math.ceil(seconds))}초 남음`;
  if (seconds < 3600) return `약 ${Math.ceil(seconds / 60)}분 남음`;
  const hours = Math.floor(seconds / 3600),
    minutes = Math.ceil((seconds % 3600) / 60);
  return `약 ${hours}시간${minutes ? ` ${minutes}분` : ""} 남음`;
};
const speed = (bps: number) =>
  bps ? `${(bps / 1024 / 1024).toFixed(1)} MB/s` : "속도 계산 중";

function Auth({ setup }: { setup: boolean }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [register, setRegister] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError("");
    setSubmitting(true);
    try {
      await api(
        setup
          ? "/api/auth/setup"
          : register
            ? "/api/auth/register"
            : "/api/auth/login",
        {
          method: "POST",
          body: JSON.stringify({
            username,
            password,
            ...(register ? { invite } : {}),
          }),
        },
      );
      window.location.reload();
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  };
  return (
    <main className="auth">
      <section className="auth-copy">
        <div className="brand">
          <span>CHZZK</span> ARCHIVE
        </div>
        <h1>
          흘러가는 라이브를
          <br />
          <em>당신의 기록</em>으로.
        </h1>
        <p>
          구독한 채널을 자동으로 기록하고, 나만의 라이브러리에서 다시 만나세요.
        </p>
        <div className="signal">
          <i /> LIVE CAPTURE SYSTEM
        </div>
      </section>
      <form className="auth-card" onSubmit={submit}>
        <small>{setup ? "FIRST RUN" : "WELCOME BACK"}</small>
        <h2>
          {setup
            ? "관리자 계정 만들기"
            : register
              ? "초대로 가입하기"
              : "아카이브에 로그인"}
        </h2>
        <p className="hint">
          {setup
            ? "이 계정이 서버의 첫 관리자가 됩니다."
            : register
              ? "받은 초대 코드로 계정을 만드세요."
              : "구독한 채널과 저장한 영상을 이어서 봅니다."}
        </p>
        <label>
          사용자 이름
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </label>
        <label>
          비밀번호
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {register && (
          <label>
            초대 코드
            <input
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              required
            />
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <Button
          type="submit"
          className="primary"
          isDisabled={submitting}
          aria-busy={submitting}
        >
          {submitting
            ? "확인 중"
            : setup
              ? "아카이브 시작"
              : register
                ? "계정 만들기"
                : "로그인"}
        </Button>
        {!setup && (
          <button
            type="button"
            className="link"
            onClick={() => {
              setRegister(!register);
              setError("");
            }}
          >
            {register ? "이미 계정이 있어요" : "초대 코드를 받았어요"}
          </button>
        )}
      </form>
    </main>
  );
}

function PageTransition({
  page,
  children,
}: {
  page: string;
  children: React.ReactNode;
}) {
  const reduceMotion = useReducedMotion();
  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        className="page-transition"
        key={page}
        initial={reduceMotion ? false : { opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        exit={reduceMotion ? { opacity: 1 } : { opacity: 0, y: -6 }}
        transition={{
          duration: motionDuration(reduceMotion, 0.22),
          ease: MOTION_EASE,
        }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

function Shell({ user }: { user: User }) {
  const location = useLocation();
  const navigate = useNavigate();
  const tab = location.pathname.split("/").filter(Boolean)[0] || "dashboard";
  const [playing, setPlaying] = useState<Recording | null>(null);
  const [playerMinimized, setPlayerMinimized] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const playerOpener = useRef<HTMLElement | null>(null);
  const reduceMotion = useReducedMotion();
  const nav = [
    { id: "dashboard", label: "대시보드", icon: LayoutDashboard },
    { id: "channels", label: "내 채널", icon: Radio },
    { id: "library", label: "라이브러리", icon: Archive },
    { id: "settings", label: "설정", icon: Settings },
    ...(user.role === "admin"
      ? [{ id: "admin", label: "관리", icon: Shield }]
      : []),
  ];
  const currentTab = nav.some((item) => item.id === tab) ? tab : "dashboard";
  const openPlayer = (recording: Recording) => {
    playerOpener.current = document.activeElement as HTMLElement | null;
    setPlayerMinimized(false);
    setPlaying(recording);
  };
  const closePlayer = () => {
    setPlayerMinimized(false);
    setPlaying(null);
  };
  const minimizePlayer = () => {
    setPlayerMinimized(true);
    window.setTimeout(() => playerOpener.current?.focus(), 0);
  };
  const goTo = (id: string) => {
    setMobileMenuOpen(false);
    navigate(id === "dashboard" ? "/" : `/${id}`);
  };
  useEffect(() => {
    if (currentTab !== tab) navigate("/", { replace: true });
  }, [currentTab, navigate, tab]);
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span>C</span> Archive
        </div>
        <nav>
          {nav.map((x) => (
            <button
              className={currentTab === x.id ? "active" : ""}
              onClick={() => goTo(x.id)}
              key={x.id}
            >
              <x.icon size={18} />
              {x.label}
            </button>
          ))}
        </nav>
        <div className="profile">
          <div>{user.username[0].toUpperCase()}</div>
          <p>
            <b>{user.username}</b>
            <small>{user.role === "admin" ? "관리자" : "사용자"}</small>
          </p>
          <button
            title="로그아웃"
            onClick={() =>
              api("/api/auth/logout", { method: "POST" }).then(() =>
                window.location.reload(),
              )
            }
          >
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <main>
        <header>
          <button
            className={`mobile-menu-button ${mobileMenuOpen ? "open" : ""}`}
            type="button"
            aria-label={mobileMenuOpen ? "메뉴 닫기" : "메뉴 열기"}
            aria-expanded={mobileMenuOpen}
            aria-controls="mobile-navigation"
            onClick={() => setMobileMenuOpen((open) => !open)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="page-title">
            <div>
              <small>PERSONAL STREAM VAULT</small>
              <h1>{nav.find((n) => n.id === currentTab)?.label}</h1>
            </div>
          </div>
          <div className={`cookie ${user.cookie_status}`}>
            <CircleDot size={15} /> 인증 쿠키{" "}
            {user.cookie_status === "valid" ? "연결됨" : "필요"}
          </div>
        </header>
        <AnimatePresence initial={false}>
          {mobileMenuOpen && (
            <motion.nav
              id="mobile-navigation"
              className="mobile-navigation"
              initial={{ height: 0, opacity: 0, y: -8 }}
              animate={{ height: "auto", opacity: 1, y: 0 }}
              exit={{ height: 0, opacity: 0, y: -8 }}
              transition={{
                duration: motionDuration(reduceMotion, 0.28),
                ease: MOTION_EASE,
              }}
            >
              <div>
                {nav.map((item) => (
                  <button
                    type="button"
                    className={currentTab === item.id ? "active" : ""}
                    onClick={() => goTo(item.id)}
                    key={item.id}
                  >
                    <item.icon size={18} />
                    {item.label}
                  </button>
                ))}
              </div>
            </motion.nav>
          )}
        </AnimatePresence>
        <PageTransition page={location.pathname}>
          {currentTab === "dashboard" ? (
            <Dashboard user={user} onPlay={openPlayer} />
          ) : currentTab === "channels" ? (
            <Channels />
          ) : currentTab === "library" ? (
            <Library user={user} onPlay={openPlayer} />
          ) : currentTab === "settings" ? (
            <SettingsPage user={user} />
          ) : (
            <Admin />
          )}
        </PageTransition>
      </main>
      <AnimatePresence
        onExitComplete={() => {
          playerOpener.current?.focus();
          playerOpener.current = null;
        }}
      >
        {playing && (
          <PlayerModal
            recording={playing}
            audioFormat={user.audio_format}
            minimized={playerMinimized}
            onMinimize={minimizePlayer}
            onRestore={() => setPlayerMinimized(false)}
            onClose={closePlayer}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

function Dashboard({
  user,
  onPlay,
}: {
  user: User;
  onPlay: (recording: Recording) => void;
}) {
  const { data: subs = [] } = useQuery({
    queryKey: ["subs"],
    queryFn: () => api<Subscription[]>("/api/subscriptions"),
  });
  const { data: recs = [] } = useQuery({
    queryKey: ["recordings"],
    queryFn: () => api<Recording[]>("/api/recordings"),
  });
  return (
    <>
      <section className="stats">
        <Stat icon={Radio} label="구독 채널" value={subs.length} />
        <Stat
          icon={CircleDot}
          label="녹화 중"
          value={recs.filter((r) => r.state === "recording").length}
          live
        />
        <Stat icon={Video} label="보관된 영상" value={recs.length} />
        <Stat
          icon={HardDrive}
          label="사용된 용량"
          value={size(recs.reduce((a, r) => a + r.size, 0))}
        />
      </section>
      <section className="section-head">
        <div>
          <small>RECENT ARCHIVES</small>
          <h2>최근 기록</h2>
        </div>
      </section>
      <RecordingGrid
        recordings={recs.slice(0, 6)}
        onPlay={onPlay}
        canPurge={user.role === "admin"}
      />
      {!recs.length && (
        <Empty text={`${user.username}님의 첫 기록을 기다리고 있어요.`} />
      )}
    </>
  );
}
function Stat({
  icon: Icon,
  label,
  value,
  live,
}: {
  icon: any;
  label: string;
  value: string | number;
  live?: boolean;
}) {
  return (
    <motion.article className="stat" layout="position">
      <span className={live ? "live" : ""}>
        <Icon size={20} />
      </span>
      <small>{label}</small>
      <strong>{value}</strong>
    </motion.article>
  );
}

function AutoRecordToggle({ subscription }: { subscription: Subscription }) {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: (enabled: boolean) =>
      api<Subscription>(`/api/subscriptions/${subscription.id}`, {
        method: "PATCH",
        body: JSON.stringify({ auto_record: enabled }),
      }),
    onMutate: async (enabled) => {
      await qc.cancelQueries({ queryKey: ["subs"] });
      const previous = qc.getQueryData<Subscription[]>(["subs"]);
      qc.setQueryData<Subscription[]>(["subs"], (current = []) =>
        current.map((item) =>
          item.id === subscription.id
            ? { ...item, auto_record: enabled }
            : item,
        ),
      );
      return { previous };
    },
    onError: (_error, _enabled, context) => {
      if (context?.previous) qc.setQueryData(["subs"], context.previous);
    },
    onSuccess: (confirmed) => {
      qc.setQueryData<Subscription[]>(["subs"], (current = []) =>
        current.map((item) =>
          item.id === confirmed.id ? { ...item, ...confirmed } : item,
        ),
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["subs"] }),
  });
  return (
    <button
      type="button"
      role="switch"
      className="auto-record-toggle"
      aria-checked={subscription.auto_record}
      aria-label={`${subscription.name} 자동 녹화`}
      aria-busy={mutation.isPending}
      disabled={mutation.isPending}
      onClick={() => mutation.mutate(!subscription.auto_record)}
    >
      <span>
        {subscription.auto_record ? "자동 녹화 켜짐" : "자동 녹화 꺼짐"}
      </span>
      <i aria-hidden="true" />
    </button>
  );
}

function Channels() {
  const qc = useQueryClient();
  const confirmAction = useConfirm();
  const [channel, setChannel] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const { data = [] } = useQuery({
    queryKey: ["subs"],
    queryFn: () => api<Subscription[]>("/api/subscriptions"),
  });
  const add = useMutation({
    mutationFn: () =>
      api<SubscribeResult>("/api/subscriptions", {
        method: "POST",
        body: JSON.stringify({ channel, auto_record: true }),
      }),
    onSuccess: async (result) => {
      const requested = channel;
      setChannel("");
      setError("");
      setNotice("");
      qc.invalidateQueries({ queryKey: ["subs"] });
      if (!result?.live) return;
      const label = result.live_title
        ? `"${result.live_title}"`
        : `${result.name} 채널`;
      if (
        !(await confirmAction({
          title: "지금부터 녹화할까요?",
          description: `${label} 방송이 진행 중입니다. 구독 시점부터 바로 녹화를 시작할 수 있습니다.`,
          confirmLabel: "녹화 시작",
        }))
      )
        return;
      try {
        await api("/api/subscriptions/start-live", {
          method: "POST",
          body: JSON.stringify({ channel: result.channel_id || requested }),
        });
        setNotice(`${result.name} 라이브 녹화를 시작했습니다.`);
        qc.invalidateQueries({ queryKey: ["recordings"] });
      } catch (e) {
        setError((e as Error).message);
      }
    },
    onError: (e) => {
      setNotice("");
      setError(e.message);
    },
  });
  return (
    <>
      <form
        className="add-bar"
        onSubmit={(e) => {
          e.preventDefault();
          if (add.isPending) return;
          add.mutate();
        }}
      >
        <div>
          <small>ADD CHANNEL</small>
          <b>치지직 채널 URL 또는 ID</b>
        </div>
        <input
          placeholder="https://chzzk.naver.com/live/..."
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
        />
        <button
          className="primary"
          disabled={add.isPending}
          aria-busy={add.isPending}
        >
          <Plus size={17} /> {add.isPending ? "확인 중" : "구독"}
        </button>
      </form>
      <AnimatePresence initial={false} mode="popLayout">
        {error && (
          <motion.p
            key="channel-error"
            className="error"
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
          >
            {error}
          </motion.p>
        )}
        {notice && (
          <motion.p
            key="channel-notice"
            className="notice"
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
          >
            {notice}
          </motion.p>
        )}
      </AnimatePresence>
      <motion.div className="channel-list" layout>
        <AnimatePresence initial={false} mode="popLayout">
          {data.map((ch) => (
            <motion.article
              key={ch.id}
              layout
              initial={{ opacity: 0, y: 9 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.98 }}
              transition={{ duration: 0.2, ease: MOTION_EASE }}
            >
              <div className="avatar">
                {ch.image ? <img src={ch.image} alt="" /> : ch.name[0]}
              </div>
              <div>
                <b>{ch.name}</b>
                <small>{ch.channel_id}</small>
              </div>
              <span className={ch.live ? "on" : ""}>
                {ch.live ? "LIVE" : "OFFLINE"}
              </span>
              <AutoRecordToggle subscription={ch} />
              <button
                onClick={async () => {
                  if (
                    !(await confirmAction({
                      title: `${ch.name} 구독을 해제할까요?`,
                      description:
                        "자동 녹화가 중단되며, 기존 영상은 다음 단계에서 유지하거나 함께 제거할 수 있습니다.",
                      confirmLabel: "구독 해제",
                      tone: "danger",
                    }))
                  )
                    return;
                  const removeRecordings = await confirmAction({
                    title: "기존 영상도 제거할까요?",
                    description:
                      "이 채널에서 저장한 기존 영상을 함께 제거할지 선택하세요.",
                    confirmLabel: "영상도 제거",
                    cancelLabel: "영상은 유지",
                    tone: "danger",
                  });
                  await api(`/api/subscriptions/${ch.id}/unsubscribe`, {
                    method: "POST",
                    body: JSON.stringify({
                      remove_recordings: removeRecordings,
                    }),
                  });
                  qc.invalidateQueries({ queryKey: ["subs"] });
                }}
              >
                구독 해제
              </button>
            </motion.article>
          ))}
        </AnimatePresence>
      </motion.div>
      <AnimatePresence initial={false}>
        {!data.length && (
          <motion.div
            key="channels-empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <Empty text="자동 녹화를 시작할 채널을 구독하세요." />
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function Library({
  user,
  onPlay,
}: {
  user: User;
  onPlay: (recording: Recording) => void;
}) {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [message, setMessage] = useState("");
  const { data = [] } = useQuery({
    queryKey: ["recordings"],
    queryFn: () => api<Recording[]>("/api/recordings"),
  });
  const download = useMutation({
    mutationFn: () =>
      api<Recording>("/api/recordings/manual", {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
    onSuccess: (r) => {
      setUrl("");
      setMessage(`${r.type.toUpperCase()} 다운로드를 시작했습니다.`);
      qc.invalidateQueries({ queryKey: ["recordings"] });
    },
    onError: (e) => setMessage(e.message),
  });
  return (
    <>
      <form
        className="add-bar download"
        onSubmit={(e) => {
          e.preventDefault();
          download.mutate();
        }}
      >
        <div>
          <small>MANUAL DOWNLOAD</small>
          <b>라이브 · VOD · 클립 저장</b>
        </div>
        <input
          placeholder="https://chzzk.naver.com/video/..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />
        <button className="primary" disabled={download.isPending}>
          <Plus size={17} />
          {download.isPending ? "확인 중" : "다운로드"}
        </button>
      </form>
      {message && <p className="notice">{message}</p>}
      <div className="section-head">
        <div>
          <small>YOUR COLLECTION</small>
          <h2>내 영상</h2>
        </div>
        <span>{data.length} ARCHIVES</span>
      </div>
      <RecordingGrid
        recordings={data}
        onPlay={onPlay}
        canPurge={user.role === "admin"}
      />
      {!data.length && <Empty text="아직 보관된 영상이 없습니다." />}
    </>
  );
}

const mediaTime = (seconds: number) => {
  if (!Number.isFinite(seconds)) return "0:00";
  const value = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
};

function ArchivePlayer({
  recording,
  audioFormat,
  onAspectRatio,
}: {
  recording: Recording;
  audioFormat: User["audio_format"];
  onAspectRatio?: (ratio: number) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const hls = useRef<HlsType | null>(null);
  const audioGraph = useRef<{
    context: AudioContext;
    compressor: DynamicsCompressorNode;
    makeup: GainNode;
    limiter: DynamicsCompressorNode;
  } | null>(null);
  const pendingMediaSwitch = useRef<{
    time: number;
    shouldPlay: boolean;
  } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(recording.duration_seconds || 0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [waiting, setWaiting] = useState(true);
  const [boostDb, setBoostDb] = useState(0);
  const [ceilingDb, setCeilingDb] = useState(-1);
  const [compression, setCompression] = useState(0);
  const [audioOnly, setAudioOnly] = useState(false);
  const [pipActive, setPipActive] = useState(false);
  const [fullscreenActive, setFullscreenActive] = useState(
    Boolean(document.fullscreenElement),
  );

  const ensureAudioGraph = () => {
    if (audioGraph.current) {
      if (audioGraph.current.context.state === "suspended")
        void audioGraph.current.context.resume();
      return audioGraph.current;
    }
    if (!video.current) return null;
    const AudioContextConstructor =
      window.AudioContext ||
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioContextConstructor) return null;
    const context = new AudioContextConstructor();
    const source = context.createMediaElementSource(video.current);
    const compressor = context.createDynamicsCompressor();
    const makeup = context.createGain();
    const limiter = context.createDynamicsCompressor();
    source
      .connect(compressor)
      .connect(makeup)
      .connect(limiter)
      .connect(context.destination);
    audioGraph.current = { context, compressor, makeup, limiter };
    void context.resume();
    return audioGraph.current;
  };

  const applyAudioEffects = (
    nextBoost: number,
    nextCeiling: number,
    nextCompression: number,
  ) => {
    const graph = ensureAudioGraph();
    if (!graph) return;
    const now = graph.context.currentTime;
    const amount = Math.max(0, Math.min(100, nextCompression)) / 100;
    graph.compressor.threshold.setTargetAtTime(
      amount ? -8 - amount * 34 : 0,
      now,
      0.02,
    );
    graph.compressor.knee.setTargetAtTime(amount * 24, now, 0.02);
    graph.compressor.ratio.setTargetAtTime(1 + amount * 11, now, 0.02);
    graph.compressor.attack.setTargetAtTime(0.004 + amount * 0.012, now, 0.02);
    graph.compressor.release.setTargetAtTime(0.18 + amount * 0.24, now, 0.02);
    graph.makeup.gain.setTargetAtTime(10 ** (nextBoost / 20), now, 0.02);
    graph.limiter.threshold.setTargetAtTime(
      nextBoost > 0 ? nextCeiling : 0,
      now,
      0.02,
    );
    graph.limiter.knee.setTargetAtTime(0, now, 0.02);
    graph.limiter.ratio.setTargetAtTime(nextBoost > 0 ? 20 : 1, now, 0.02);
    graph.limiter.attack.setTargetAtTime(0.002, now, 0.02);
    graph.limiter.release.setTargetAtTime(0.12, now, 0.02);
  };

  const changeBoost = (value: number) => {
    setBoostDb(value);
    applyAudioEffects(value, ceilingDb, compression);
  };
  const changeCeiling = (value: number) => {
    setCeilingDb(value);
    applyAudioEffects(boostDb, value, compression);
  };
  const changeCompression = (value: number) => {
    setCompression(value);
    applyAudioEffects(boostDb, ceilingDb, value);
  };

  useEffect(
    () => () => {
      const context = audioGraph.current?.context;
      audioGraph.current = null;
      if (context && context.state !== "closed") void context.close();
    },
    [],
  );

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (audioOnly && element.currentSrc && !pendingMediaSwitch.current) {
      pendingMediaSwitch.current = {
        time: element.currentTime,
        shouldPlay: !element.paused,
      };
    }
    hls.current?.destroy();
    hls.current = null;
    let disposed = false;
    let instance: HlsType | null = null;
    const source = audioOnly
      ? `/api/media/${recording.id}/audio?format=${audioFormat}`
      : `/api/hls/${recording.id}/master.m3u8`;
    if (audioOnly || element.canPlayType("application/vnd.apple.mpegurl")) {
      element.src = source;
      element.load();
      return;
    }
    void import("hls.js").then(({ default: Hls }) => {
      if (disposed) return;
      if (!Hls.isSupported()) {
        setWaiting(false);
        return;
      }
      instance = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 60,
      });
      hls.current = instance;
      instance.attachMedia(element);
      instance.on(Hls.Events.MEDIA_ATTACHED, () =>
        instance?.loadSource(source),
      );
      instance.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) setWaiting(false);
      });
    });
    return () => {
      disposed = true;
      instance?.destroy();
      if (hls.current === instance) hls.current = null;
    };
  }, [audioFormat, audioOnly, recording.id]);
  const toggle = () => {
    const element = video.current;
    if (!element) return;
    if (element.paused) void element.play();
    else element.pause();
  };
  const fullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      // Fullscreen can be rejected by browser policy; keep playback intact.
    }
  };
  const togglePictureInPicture = async () => {
    const element = video.current;
    if (!element || audioOnly || !document.pictureInPictureEnabled) return;
    try {
      if (document.pictureInPictureElement)
        await document.exitPictureInPicture();
      else await element.requestPictureInPicture();
    } catch {
      // The browser may reject PIP before enough media has loaded.
    }
  };
  const toggleAudioOnly = () => {
    const element = video.current;
    if (!element) return;
    if (document.pictureInPictureElement === element)
      void document.exitPictureInPicture();
    pendingMediaSwitch.current = {
      time: element.currentTime,
      shouldPlay: !element.paused,
    };
    setWaiting(true);
    setAudioOnly((enabled) => !enabled);
  };

  useEffect(() => {
    const handleFullscreen = () =>
      setFullscreenActive(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", handleFullscreen);
    return () =>
      document.removeEventListener("fullscreenchange", handleFullscreen);
  }, []);

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    const entered = () => setPipActive(true);
    const left = () => setPipActive(false);
    element.addEventListener("enterpictureinpicture", entered);
    element.addEventListener("leavepictureinpicture", left);
    return () => {
      element.removeEventListener("enterpictureinpicture", entered);
      element.removeEventListener("leavepictureinpicture", left);
    };
  }, []);
  const seek = (value: number) => {
    if (video.current) video.current.currentTime = value;
    setCurrent(value);
  };
  const changeVolume = (value: number) => {
    if (!video.current) return;
    video.current.volume = value;
    video.current.muted = value === 0;
    setVolume(value);
    setMuted(value === 0);
  };
  const toggleMute = () => {
    if (!video.current) return;
    video.current.muted = !video.current.muted;
    setMuted(video.current.muted);
  };
  return (
    <div
      className={`archive-player ${audioOnly ? "audio-only" : ""}`}
      ref={frame}
      onDoubleClick={fullscreen}
    >
      <video
        ref={video}
        autoPlay
        playsInline
        onClick={toggle}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrent(event.currentTarget.currentTime)}
        onDurationChange={(event) => {
          const measured = event.currentTarget.duration;
          if (Number.isFinite(measured) && measured > 0) setDuration(measured);
        }}
        onLoadedMetadata={(event) => {
          const element = event.currentTarget;
          const pending = pendingMediaSwitch.current;
          if (pending) {
            element.currentTime = Math.min(
              pending.time,
              Number.isFinite(element.duration)
                ? element.duration
                : pending.time,
            );
            setCurrent(element.currentTime);
            pendingMediaSwitch.current = null;
            if (pending.shouldPlay) void element.play();
          }
          const { videoWidth, videoHeight } = element;
          if (videoWidth > 0 && videoHeight > 0)
            onAspectRatio?.(videoWidth / videoHeight);
        }}
        onLoadStart={() => setWaiting(true)}
        onCanPlay={() => setWaiting(false)}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
      />
      {audioOnly && (
        <div className="audio-only-visual" aria-live="polite">
          <Headphones />
          <strong>라디오 모드</strong>
          <span>{audioFormat.toUpperCase()} 오디오만 전송 중</span>
        </div>
      )}
      {waiting && (
        <span className="player-spinner" aria-label="영상 불러오는 중" />
      )}
      {!playing && !waiting && (
        <button className="player-center" onClick={toggle} aria-label="재생">
          <Play fill="currentColor" />
        </button>
      )}
      <div
        className="player-controls"
        onDoubleClick={(event) => event.stopPropagation()}
      >
        <input
          className="player-seek"
          type="range"
          min="0"
          max={duration || 0}
          step="0.1"
          value={Math.min(current, duration || 0)}
          onChange={(event) => seek(Number(event.target.value))}
          style={
            {
              "--progress": `${duration ? (current / duration) * 100 : 0}%`,
            } as React.CSSProperties
          }
          aria-label="재생 위치"
        />
        <div className="player-buttons">
          <button onClick={toggle} aria-label={playing ? "일시정지" : "재생"}>
            {playing ? (
              <Pause fill="currentColor" />
            ) : (
              <Play fill="currentColor" />
            )}
          </button>
          <button
            onClick={toggleMute}
            aria-label={muted ? "음소거 해제" : "음소거"}
          >
            {muted || volume === 0 ? <VolumeX /> : <Volume2 />}
          </button>
          <input
            className="player-volume"
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={muted ? 0 : volume}
            onChange={(event) => changeVolume(Number(event.target.value))}
            aria-label="볼륨"
          />
          <div className="audio-tool">
            <button
              type="button"
              className={boostDb > 0 ? "active" : ""}
              onClick={() => changeBoost(boostDb > 0 ? 0 : 6)}
              aria-pressed={boostDb > 0}
              aria-label={
                boostDb > 0 ? "오디오 최대화 끄기" : "오디오 최대화 켜기"
              }
            >
              <Gauge />
            </button>
            <div
              className="audio-popover"
              role="group"
              aria-label="오디오 최대화 설정"
            >
              <header>
                <b>오디오 최대화</b>
                <output>
                  {boostDb > 0 ? `+${boostDb.toFixed(1)} dB` : "꺼짐"}
                </output>
              </header>
              <label>
                <span>증폭</span>
                <input
                  type="range"
                  min="0"
                  max="12"
                  step="0.5"
                  value={boostDb}
                  onChange={(event) => changeBoost(Number(event.target.value))}
                  aria-label="오디오 최대화 증폭"
                />
              </label>
              <label>
                <span>
                  피크 목표 <output>{ceilingDb.toFixed(1)} dB</output>
                </span>
                <input
                  type="range"
                  min="-6"
                  max="0"
                  step="0.5"
                  value={ceilingDb}
                  onChange={(event) =>
                    changeCeiling(Number(event.target.value))
                  }
                  aria-label="최대 피크 목표"
                />
              </label>
            </div>
          </div>
          <div className="audio-tool">
            <button
              type="button"
              className={compression > 0 ? "active" : ""}
              onClick={() => changeCompression(compression > 0 ? 0 : 45)}
              aria-pressed={compression > 0}
              aria-label={
                compression > 0
                  ? "오디오 컴프레서 끄기"
                  : "오디오 컴프레서 켜기"
              }
            >
              <AudioLines />
            </button>
            <div
              className="audio-popover compressor-popover"
              role="group"
              aria-label="오디오 컴프레서 설정"
            >
              <header>
                <b>컴프레서</b>
                <output>{compression > 0 ? `${compression}%` : "꺼짐"}</output>
              </header>
              <label>
                <span>강도</span>
                <input
                  type="range"
                  min="0"
                  max="100"
                  step="5"
                  value={compression}
                  onChange={(event) =>
                    changeCompression(Number(event.target.value))
                  }
                  aria-label="컴프레서 강도"
                />
              </label>
              <p>큰 소리는 누르고 작은 소리는 더 또렷하게 유지합니다.</p>
            </div>
          </div>
          <span>
            {mediaTime(current)} / {mediaTime(duration)}
          </span>
          <button
            type="button"
            className={`player-radio-toggle ${audioOnly ? "active" : ""}`}
            onClick={toggleAudioOnly}
            aria-pressed={audioOnly}
            aria-label={audioOnly ? "영상 모드로 전환" : "라디오 모드로 전환"}
            title={audioOnly ? "영상 모드" : "라디오 모드"}
          >
            <Headphones />
          </button>
          <button
            type="button"
            className={pipActive ? "active" : ""}
            onClick={togglePictureInPicture}
            disabled={audioOnly || !document.pictureInPictureEnabled}
            aria-pressed={pipActive}
            aria-label={pipActive ? "PIP 종료" : "PIP 시작"}
            title={
              audioOnly ? "라디오 모드에서는 PIP를 사용할 수 없습니다" : "PIP"
            }
          >
            <PictureInPicture2 />
          </button>
          <button
            className="player-fullscreen"
            onClick={fullscreen}
            aria-label={fullscreenActive ? "전체 화면 종료" : "전체 화면"}
            aria-pressed={fullscreenActive}
          >
            <Maximize />
          </button>
        </div>
      </div>
    </div>
  );
}

function PlayerModal({
  recording,
  audioFormat,
  minimized,
  onMinimize,
  onRestore,
  onClose,
}: {
  recording: Recording;
  audioFormat: User["audio_format"];
  minimized: boolean;
  onMinimize: () => void;
  onRestore: () => void;
  onClose: () => void;
}) {
  const overlay = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const resizing = useRef(false);
  const dragControls = useDragControls();
  const dragX = useMotionValue(0);
  const dragY = useMotionValue(0);
  const reduceMotion = useReducedMotion();
  const [fullscreenActive, setFullscreenActive] = useState(
    Boolean(document.fullscreenElement),
  );
  const [aspectRatio, setAspectRatio] = useState(16 / 9);
  const initialSize = () => ({
    width: Math.min(1000, window.innerWidth - 24),
    height: Math.min(720, window.innerHeight - 24),
  });
  const [dialogSize, setDialogSize] = useState(initialSize);

  const layoutSize = (element: HTMLElement) => {
    const styles = window.getComputedStyle(element);
    return {
      width: Number.parseFloat(styles.width),
      height: Number.parseFloat(styles.height),
    };
  };

  useEffect(() => {
    const clamp = () =>
      setDialogSize((current) => ({
        width: Math.min(current.width, window.innerWidth - 24),
        height: Math.min(current.height, window.innerHeight - 24),
      }));
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!document.fullscreenElement) onClose();
        return;
      }
      if (minimized || event.key !== "Tab" || !dialog.current) return;
      const focusable = Array.from(
        dialog.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("hidden"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const previousOverflow = document.body.style.overflow;
    if (!minimized) {
      document.body.style.overflow = "hidden";
      closeButton.current?.focus();
    }
    window.addEventListener("resize", clamp);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("resize", clamp);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [minimized, onClose]);

  useEffect(() => {
    const handleFullscreen = () =>
      setFullscreenActive(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", handleFullscreen);
    return () =>
      document.removeEventListener("fullscreenchange", handleFullscreen);
  }, []);

  useEffect(() => {
    dragX.set(0);
    dragY.set(0);
  }, [dragX, dragY, fullscreenActive, minimized]);

  const fitDialogToVideo = (
    requestedPlayerWidth: number,
    ratio: number,
    chromeWidth: number,
    chromeHeight: number,
  ) => {
    const maxDialogWidth = Math.max(280, window.innerWidth - 24);
    const maxDialogHeight = Math.max(240, window.innerHeight - 24);
    const minDialogWidth = Math.min(360, maxDialogWidth);
    const minDialogHeight = Math.min(300, maxDialogHeight);
    const maxPlayerWidth = Math.max(
      1,
      Math.min(
        maxDialogWidth - chromeWidth,
        (maxDialogHeight - chromeHeight) * ratio,
      ),
    );
    const minPlayerWidth = Math.min(
      maxPlayerWidth,
      Math.max(
        1,
        minDialogWidth - chromeWidth,
        (minDialogHeight - chromeHeight) * ratio,
      ),
    );
    const playerWidth = Math.min(
      maxPlayerWidth,
      Math.max(minPlayerWidth, requestedPlayerWidth),
    );
    return {
      width: playerWidth + chromeWidth,
      height: playerWidth / ratio + chromeHeight,
    };
  };

  useEffect(() => {
    let frame = 0;
    const applyRatio = () => {
      frame = 0;
      if (resizing.current || minimized || fullscreenActive) return;
      const dialogElement = dialog.current;
      const playerElement =
        dialogElement?.querySelector<HTMLElement>(".archive-player");
      if (!dialogElement || !playerElement) return;
      const bounds = layoutSize(dialogElement);
      const playerBounds = layoutSize(playerElement);
      const next = fitDialogToVideo(
        playerBounds.width,
        aspectRatio,
        bounds.width - playerBounds.width,
        bounds.height - playerBounds.height,
      );
      setDialogSize((current) =>
        Math.abs(current.width - next.width) < 0.01 &&
        Math.abs(current.height - next.height) < 0.01
          ? current
          : next,
      );
    };
    const scheduleRatio = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(applyRatio);
    };
    const dialogElement = dialog.current;
    const playerElement =
      dialogElement?.querySelector<HTMLElement>(".archive-player");
    if (!dialogElement || !playerElement) return;
    const observer = new ResizeObserver(scheduleRatio);
    observer.observe(dialogElement);
    observer.observe(playerElement);
    scheduleRatio();
    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [aspectRatio, fullscreenActive, minimized]);

  const enforceAspectRatio = (ratio: number) => {
    if (!Number.isFinite(ratio) || ratio <= 0) return;
    setAspectRatio(ratio);
  };

  const startResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (minimized || fullscreenActive) return;
    event.preventDefault();
    event.stopPropagation();
    const dialogElement = dialog.current;
    const playerElement =
      dialogElement?.querySelector<HTMLElement>(".archive-player");
    if (!dialogElement || !playerElement) return;
    const bounds = layoutSize(dialogElement);
    const playerBounds = layoutSize(playerElement);
    resizing.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    const origin = {
      x: event.clientX,
      y: event.clientY,
      playerWidth: playerBounds.width,
      chromeWidth: bounds.width - playerBounds.width,
      chromeHeight: bounds.height - playerBounds.height,
    };
    const move = (pointer: PointerEvent) => {
      const horizontalDelta = pointer.clientX - origin.x;
      const verticalDelta = (pointer.clientY - origin.y) * aspectRatio;
      const dominantDelta =
        Math.abs(horizontalDelta) >= Math.abs(verticalDelta)
          ? horizontalDelta
          : verticalDelta;
      setDialogSize(
        fitDialogToVideo(
          origin.playerWidth + dominantDelta,
          aspectRatio,
          origin.chromeWidth,
          origin.chromeHeight,
        ),
      );
    };
    const stop = () => {
      resizing.current = false;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      document.body.classList.remove("resizing-player");
    };
    document.body.classList.add("resizing-player");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
    window.addEventListener("pointercancel", stop, { once: true });
  };

  const startDrag = (event: React.PointerEvent<HTMLElement>) => {
    if (fullscreenActive || (event.target as HTMLElement).closest("button"))
      return;
    dragControls.start(event);
  };

  const closePlayer = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    onClose();
  };

  const minimizedSize = {
    width: Math.min(380, window.innerWidth - 24),
    height: Math.min(280, window.innerHeight - 24),
  };

  return (
    <motion.div
      ref={overlay}
      className={`player-overlay ${minimized ? "minimized" : ""}`}
    >
      <AnimatePresence>
        {!minimized && (
          <motion.div
            className="player-backdrop"
            aria-hidden="true"
            onClick={() => !resizing.current && onMinimize()}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{
              duration: motionDuration(reduceMotion, 0.2),
              ease: MOTION_EASE,
            }}
          />
        )}
      </AnimatePresence>
      <motion.section
        ref={dialog}
        className={`player-dialog ${minimized ? "minimized" : ""}`}
        role="dialog"
        aria-modal={!minimized}
        aria-labelledby="player-dialog-title"
        style={{
          width: minimized ? minimizedSize.width : dialogSize.width,
          height: minimized ? minimizedSize.height : dialogSize.height,
          x: dragX,
          y: dragY,
        }}
        drag={!fullscreenActive}
        dragListener={false}
        dragControls={dragControls}
        dragConstraints={overlay}
        dragElastic={0.04}
        dragMomentum={false}
        onClick={(event) => event.stopPropagation()}
        initial={reduceMotion ? false : { opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.98 }}
        transition={{
          duration: motionDuration(reduceMotion, 0.22),
          ease: MOTION_EASE,
        }}
      >
        <div
          className="player-dialog-head"
          onPointerDown={startDrag}
          title="드래그하여 플레이어 이동"
        >
          <div>
            <small>
              {recording.channel} · {typeLabel(recording.type)}
            </small>
            <strong id="player-dialog-title">{recording.title}</strong>
          </div>
          <div className="player-window-actions">
            {minimized && (
              <button
                onClick={onRestore}
                aria-label="플레이어 원래 크기로 복원"
                title="원래 크기로"
              >
                <Maximize2 />
              </button>
            )}
            <button
              ref={closeButton}
              onClick={closePlayer}
              aria-label="플레이어 닫기"
            >
              ✕
            </button>
          </div>
        </div>
        <div className="player-dialog-media">
          <ArchivePlayer
            recording={recording}
            audioFormat={audioFormat}
            onAspectRatio={enforceAspectRatio}
          />
        </div>
        <div className="player-dialog-foot">
          {new Date(recording.created_at).toLocaleString("ko-KR")} ·{" "}
          {size(recording.size)}
        </div>
        <button
          className="player-resize"
          onPointerDown={startResize}
          aria-label="플레이어 크기 조절"
          title="드래그하여 크기 조절"
        />
      </motion.section>
    </motion.div>
  );
}

function RecordingGrid({
  recordings,
  onPlay,
  canPurge = false,
}: {
  recordings: Recording[];
  onPlay: (recording: Recording) => void;
  canPurge?: boolean;
}) {
  const qc = useQueryClient();
  const confirmAction = useConfirm();
  const [pendingIds, setPendingIds] = useState<Set<number>>(() => new Set());
  const setPending = (id: number, pending: boolean) =>
    setPendingIds((current) => {
      const next = new Set(current);
      if (pending) next.add(id);
      else next.delete(id);
      return next;
    });
  const cancel = async (r: Recording) => {
    if (
      await confirmAction({
        title:
          r.type === "live" ? "녹화를 중단할까요?" : "다운로드를 취소할까요?",
        description:
          "현재까지 저장된 데이터는 작업 상태에 따라 일부 남을 수 있습니다.",
        confirmLabel: r.type === "live" ? "녹화 중단" : "다운로드 취소",
        tone: "danger",
      })
    ) {
      setPending(r.id, true);
      try {
        await api(`/api/recordings/${r.id}/cancel`, { method: "POST" });
        qc.setQueryData<Recording[]>(["recordings"], (current = []) =>
          current.map((item) =>
            item.id === r.id ? { ...item, state: "canceled" } : item,
          ),
        );
        await qc.invalidateQueries({ queryKey: ["recordings"] });
      } finally {
        setPending(r.id, false);
      }
    }
  };
  const remove = async (r: Recording) => {
    if (
      !(await confirmAction({
        title: "내 라이브러리에서 제거할까요?",
        description:
          "내 목록에서만 사라지며 다른 사용자의 아카이브에는 영향을 주지 않습니다.",
        confirmLabel: "목록에서 제거",
        tone: "danger",
      }))
    )
      return;
    setPending(r.id, true);
    try {
      await api(`/api/recordings/${r.id}`, { method: "DELETE" });
      qc.setQueryData<Recording[]>(["recordings"], (current = []) =>
        current.filter((item) => item.id !== r.id),
      );
    } finally {
      setPending(r.id, false);
    }
  };
  const purge = async (r: Recording) => {
    if (
      !(await confirmAction({
        title: `“${r.title}”을 영구 삭제할까요?`,
        description:
          "모든 사용자의 라이브러리에서 제거되고 영상 파일도 삭제됩니다. 이 작업은 되돌릴 수 없습니다.",
        confirmLabel: "영구 삭제",
        tone: "danger",
      }))
    )
      return;
    setPending(r.id, true);
    try {
      await api(`/api/admin/recordings/${r.id}`, { method: "DELETE" });
      qc.setQueryData<Recording[]>(["recordings"], (current = []) =>
        current.filter((item) => item.id !== r.id),
      );
      qc.invalidateQueries({ queryKey: ["admin"] });
    } finally {
      setPending(r.id, false);
    }
  };
  return (
    <motion.div className="recording-grid" layout>
      <AnimatePresence initial={false} mode="popLayout">
        {recordings.map((r) => {
          const progressValue =
            r.state === "processing"
              ? r.encoding_progress != null && r.encoding_progress > 0
                ? r.encoding_progress
                : undefined
              : r.progress;
          return (
            <motion.article
              key={r.id}
              layout
              initial={{ opacity: 0, y: 9 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.98 }}
              whileHover={{ y: -3 }}
              transition={{ duration: 0.2, ease: MOTION_EASE }}
            >
              <div
                className={`thumb ${r.state === "completed" ? "viewable" : ""}`}
                style={
                  r.thumbnail ? { backgroundImage: `url(${r.thumbnail})` } : {}
                }
                onClick={() => r.state === "completed" && onPlay(r)}
                onKeyDown={(event) => {
                  if (
                    r.state === "completed" &&
                    (event.key === "Enter" || event.key === " ")
                  ) {
                    event.preventDefault();
                    onPlay(r);
                  }
                }}
                role={r.state === "completed" ? "button" : undefined}
                tabIndex={r.state === "completed" ? 0 : undefined}
                aria-label={
                  r.state === "completed" ? `${r.title} 재생` : undefined
                }
              >
                <span className={r.state}>
                  {typeLabel(r.type)} · {stateLabel(r)}
                </span>
                {r.state === "completed" && (
                  <button
                    className="play"
                    onClick={(event) => {
                      event.stopPropagation();
                      onPlay(r);
                    }}
                    aria-label="영상 재생"
                  >
                    ▶
                  </button>
                )}
              </div>
              <div className="recording-meta">
                <small>{r.channel}</small>
                <h3>{r.title}</h3>
                {(r.state === "recording" ||
                  r.state === "queued" ||
                  r.state === "processing") && (
                  <div
                    className={`download-progress ${r.type === "live" ? "live-recording-progress" : ""}`}
                  >
                    {r.type === "live" && r.state !== "processing" && (
                      <div className="live-recording-status">
                        <div>
                          <span
                            className={
                              r.recording_active && r.speed_bps > 0
                                ? "healthy"
                                : "connecting"
                            }
                          >
                            {r.state === "queued"
                              ? "녹화 대기 중"
                              : r.recording_active && r.speed_bps > 0
                                ? "실제 녹화 중"
                                : r.recording_active
                                  ? "스트림 연결 중"
                                  : "프로세스 확인 중"}
                          </span>
                          <em aria-hidden="true">|</em>
                          <strong>{mediaTime(r.recorded_seconds || 0)}</strong>
                        </div>
                        <div>
                          <span>
                            {size(r.size)} 저장됨
                            {r.speed_bps > 0 ? ` · ${speed(r.speed_bps)}` : ""}
                          </span>
                        </div>
                        <div
                          className={`live-recording-meter ${r.recording_active ? "writing" : ""}`}
                        >
                          <i />
                        </div>
                      </div>
                    )}
                    <div>
                      <span>
                        {r.state === "processing"
                          ? r.encoding_state === "finalizing"
                            ? "MP4 마무리 중"
                            : r.encoding_state === "uploading"
                              ? "결과 전송 완료"
                              : r.encoding_progress != null &&
                                  r.encoding_progress > 0
                                ? `HEVC 인코딩 ${r.encoding_progress.toFixed(1)}%`
                                : `HEVC 인코딩 중 · ${mediaTime(r.encoding_processed_seconds || 0)}`
                          : r.progress != null
                            ? `${r.progress.toFixed(1)}%`
                            : "연결 중"}
                      </span>
                      <span>
                        {size(r.size)}
                        {r.total_size ? ` / ${size(r.total_size)}` : ""}
                      </span>
                    </div>
                    <progress
                      max="100"
                      className={
                        progressValue == null ? "indeterminate" : undefined
                      }
                      value={progressValue}
                      aria-label={
                        progressValue == null
                          ? "진행 중"
                          : `진행률 ${progressValue.toFixed(1)}%`
                      }
                    />
                    <div className="download-eta">
                      <span>
                        {r.state === "processing"
                          ? r.encoding_state === "finalizing"
                            ? "컨트롤러 처리 중"
                            : r.encoding_speed && r.encoding_speed > 0
                              ? `${r.encoding_speed.toFixed(2)}x 속도`
                              : "인코더 준비 중"
                          : speed(r.speed_bps)}
                      </span>
                      <span>
                        {r.state === "processing"
                          ? r.encoding_state === "finalizing"
                            ? "잠시만 기다려 주세요"
                            : r.encoding_eta_seconds != null
                              ? eta(r.encoding_eta_seconds)
                              : "남은 시간 계산 중"
                          : eta(r.eta_seconds)}
                      </span>
                    </div>
                  </div>
                )}
                <p>
                  {new Date(r.created_at).toLocaleString("ko-KR")} ·{" "}
                  {size(r.size)}
                </p>
                {r.error && <p className="error">{r.error}</p>}
                <div className="recording-actions">
                  {(r.state === "recording" ||
                    r.state === "queued" ||
                    r.state === "processing") && (
                    <button
                      className="cancel"
                      disabled={pendingIds.has(r.id)}
                      onClick={() => cancel(r)}
                    >
                      {r.state === "processing"
                        ? "인코딩 취소"
                        : r.type === "live"
                          ? "녹화 중단"
                          : "다운로드 취소"}
                    </button>
                  )}
                  <button
                    disabled={pendingIds.has(r.id)}
                    onClick={() => remove(r)}
                  >
                    내 기록에서 제거
                  </button>
                  {canPurge && !IN_FLIGHT_STATES.has(r.state) && (
                    <button
                      className="purge"
                      disabled={pendingIds.has(r.id)}
                      onClick={() => purge(r)}
                    >
                      <Trash2 size={13} /> 아카이브 삭제
                    </button>
                  )}
                </div>
              </div>
            </motion.article>
          );
        })}
      </AnimatePresence>
    </motion.div>
  );
}
function SettingsPage({ user }: { user: User }) {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const audioPreference = useMutation({
    mutationFn: (audioFormat: User["audio_format"]) =>
      api<{ audio_format: User["audio_format"] }>("/api/me/preferences", {
        method: "PATCH",
        body: JSON.stringify({ audio_format: audioFormat }),
      }),
    onMutate: async (audioFormat) => {
      await qc.cancelQueries({ queryKey: ["me"] });
      const previous = qc.getQueryData<User>(["me"]);
      qc.setQueryData<User>(["me"], (current) =>
        current ? { ...current, audio_format: audioFormat } : current,
      );
      return { previous };
    },
    onError: (_error, _audioFormat, context) => {
      if (context?.previous) qc.setQueryData(["me"], context.previous);
    },
    onSuccess: ({ audio_format }) => {
      qc.setQueryData<User>(["me"], (current) =>
        current ? { ...current, audio_format } : current,
      );
    },
  });
  return (
    <div className="settings-grid">
      <article>
        <small>CHROME EXTENSION</small>
        <h2>브라우저 연결</h2>
        <p>
          치지직 로그인 쿠키를 안전하게 동기화합니다. 코드는 10분 동안 한 번만
          사용할 수 있습니다.
        </p>
        {code ? (
          <code>{code}</code>
        ) : (
          <button
            className="primary"
            onClick={() =>
              api<{ code: string }>("/api/me/pair", { method: "POST" }).then(
                (x) => setCode(x.code),
              )
            }
          >
            페어링 코드 만들기
          </button>
        )}
      </article>
      <article>
        <small>RADIO AUDIO</small>
        <h2>라디오 모드 형식</h2>
        <p>
          AAC는 데이터 사용량이 적고 빠릅니다. FLAC은 24-bit FLAC 파일을
          재인코딩 없이 전송합니다.
        </p>
        <div
          className="audio-format-control"
          role="radiogroup"
          aria-label="라디오 모드 스트림 형식"
        >
          {(["aac", "flac"] as const).map((format) => (
            <button
              type="button"
              role="radio"
              aria-checked={user.audio_format === format}
              className={user.audio_format === format ? "active" : ""}
              disabled={audioPreference.isPending}
              onClick={() => audioPreference.mutate(format)}
              key={format}
            >
              {format.toUpperCase()}
            </button>
          ))}
        </div>
      </article>
      <article>
        <small>SECURITY</small>
        <h2>개인 라이브러리</h2>
        <p>
          영상 접근 권한은 계정별로 분리됩니다. 같은 방송을 다른 사용자가
          구독해도 실제 파일은 하나만 저장됩니다.
        </p>
      </article>
    </div>
  );
}
function Admin() {
  const { data } = useQuery({
    queryKey: ["admin"],
    queryFn: () => api<any>("/api/admin/overview"),
  });
  const [invite, setInvite] = useState("");
  if (!data) return null;
  return (
    <>
      <section className="stats">
        <Stat icon={Users} label="사용자" value={data.users} />
        <Stat icon={Radio} label="활성 구독" value={data.subscriptions} />
        <Stat icon={Video} label="공용 녹화" value={data.recordings} />
        <Stat
          icon={HardDrive}
          label="디스크 사용"
          value={`${data.disk.percent}%`}
        />
      </section>
      <div className="settings-grid admin-invites">
        <article>
          <small>INVITATIONS</small>
          <h2>사용자 초대</h2>
          <p>24시간 동안 유효한 일회용 초대 코드를 발급합니다.</p>
          {invite ? (
            <code>{invite}</code>
          ) : (
            <button
              className="primary"
              onClick={() =>
                api<{ token: string }>("/api/admin/invites", {
                  method: "POST",
                }).then((x) => setInvite(x.token))
              }
            >
              초대 코드 발급
            </button>
          )}
        </article>
      </div>
    </>
  );
}
function Empty({ text }: { text: string }) {
  return (
    <div className="empty">
      <Archive size={36} />
      <p>{text}</p>
    </div>
  );
}

function App() {
  const { data: status, isLoading } = useQuery({
    queryKey: ["status"],
    queryFn: () => api<{ setup_required: boolean }>("/api/auth/status"),
  });
  const { data: user } = useQuery({
    queryKey: ["me"],
    queryFn: () => api<User>("/api/me"),
    retry: false,
    enabled: status && !status.setup_required,
  });
  if (isLoading) return null;
  if (status?.setup_required) return <Auth setup />;
  // A background auth refresh can fail briefly while the server restarts. Keep
  // the last successful session rendered; a fresh load with no user still
  // lands on the login screen.
  if (!user) return <Auth setup={false} />;
  return <Shell user={user} />;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Keep the last successful value visible while a background poll is in
      // flight. Stable item IDs then let React update only changed content.
      refetchInterval: (query) =>
        hasActiveWork(query.state.data) ? 1000 : 15000,
      refetchIntervalInBackground: false,
      staleTime: 800,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <MotionConfig reducedMotion="user">
        <QueryClientProvider client={queryClient}>
          <ConfirmProvider>
            <App />
          </ConfirmProvider>
        </QueryClientProvider>
      </MotionConfig>
    </BrowserRouter>
  </React.StrictMode>,
);
