import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Button } from "@heroui/react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Archive,
  CircleDot,
  HardDrive,
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
} from "lucide-react";
import "./styles.css";
import "./player.css";

type User = {
  id: number;
  username: string;
  role: "admin" | "user";
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
  recorded_seconds: number;
  recording_active: boolean;
  created_at: string;
  error?: string;
};

const IN_FLIGHT_STATES = new Set(["queued", "recording", "processing"]);

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
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
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
      location.reload();
    } catch (e) {
      setError((e as Error).message);
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
        <Button type="submit" className="primary">
          {setup ? "아카이브 시작" : register ? "계정 만들기" : "로그인"}
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
        initial={
          reduceMotion ? false : { opacity: 0, y: 14, filter: "blur(4px)" }
        }
        animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
        exit={
          reduceMotion
            ? { opacity: 1 }
            : { opacity: 0, y: -8, filter: "blur(3px)" }
        }
        transition={{
          duration: reduceMotion ? 0 : 0.24,
          ease: [0.22, 1, 0.36, 1],
        }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

function Shell({ user }: { user: User }) {
  const [tab, setTab] = useState("dashboard");
  const [playing, setPlaying] = useState<Recording | null>(null);
  const nav = [
    { id: "dashboard", label: "대시보드", icon: LayoutDashboard },
    { id: "channels", label: "내 채널", icon: Radio },
    { id: "library", label: "라이브러리", icon: Archive },
    { id: "settings", label: "설정", icon: Settings },
    ...(user.role === "admin"
      ? [{ id: "admin", label: "관리", icon: Shield }]
      : []),
  ];
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span>C</span> Archive
        </div>
        <nav>
          {nav.map((x) => (
            <button
              className={tab === x.id ? "active" : ""}
              onClick={() => {
                setPlaying(null);
                setTab(x.id);
              }}
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
                location.reload(),
              )
            }
          >
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <main>
        <PageTransition page={tab}>
          <header>
            <div>
              <small>PERSONAL STREAM VAULT</small>
              <h1>{nav.find((n) => n.id === tab)?.label}</h1>
            </div>
            <div className={`cookie ${user.cookie_status}`}>
              <CircleDot size={15} /> 인증 쿠키{" "}
              {user.cookie_status === "valid" ? "연결됨" : "필요"}
            </div>
          </header>
          {tab === "dashboard" ? (
            <Dashboard user={user} onPlay={setPlaying} />
          ) : tab === "channels" ? (
            <Channels />
          ) : tab === "library" ? (
            <Library user={user} onPlay={setPlaying} />
          ) : tab === "settings" ? (
            <SettingsPage />
          ) : (
            <Admin />
          )}
        </PageTransition>
      </main>
      <AnimatePresence>
        {playing && <PlayerModal recording={playing} onClose={() => setPlaying(null)} />}
      </AnimatePresence>
    </div>
  );
}

function Dashboard({ user, onPlay }: { user: User; onPlay: (recording: Recording) => void }) {
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
      <RecordingGrid recordings={recs.slice(0, 6)} onPlay={onPlay} canPurge={user.role === "admin"} />
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
    <article className="stat">
      <span className={live ? "live" : ""}>
        <Icon size={20} />
      </span>
      <small>{label}</small>
      <strong>{value}</strong>
    </article>
  );
}

function Channels() {
  const qc = useQueryClient();
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
      if (!confirm(`${label} 방송이 진행 중입니다. 지금부터 바로 녹화할까요?`)) return;
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
        <button className="primary">
          <Plus size={17} /> 구독
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}
      <div className="channel-list">
        {data.map((ch) => (
          <article key={ch.id}>
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
            <button
              onClick={async () => {
                if (confirm("기존 영상도 라이브러리에서 제거할까요?"))
                  await api(`/api/subscriptions/${ch.id}/unsubscribe`, {
                    method: "POST",
                    body: JSON.stringify({ remove_recordings: true }),
                  });
                else
                  await api(`/api/subscriptions/${ch.id}/unsubscribe`, {
                    method: "POST",
                    body: JSON.stringify({ remove_recordings: false }),
                  });
                qc.invalidateQueries({ queryKey: ["subs"] });
              }}
            >
              구독 해제
            </button>
          </article>
        ))}
      </div>
      {!data.length && <Empty text="자동 녹화를 시작할 채널을 구독하세요." />}
    </>
  );
}

function Library({ user, onPlay }: { user: User; onPlay: (recording: Recording) => void }) {
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
      <RecordingGrid recordings={data} onPlay={onPlay} canPurge={user.role === "admin"} />
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

function ArchivePlayer({ recording, onAspectRatio }: { recording: Recording; onAspectRatio?: (ratio: number) => void }) {
  const frame = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [waiting, setWaiting] = useState(true);
  const toggle = () => {
    const element = video.current;
    if (!element) return;
    if (element.paused) void element.play();
    else element.pause();
  };
  const fullscreen = () => {
    if (!frame.current) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void frame.current.requestFullscreen();
  };
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
    <div className="archive-player" ref={frame} onDoubleClick={fullscreen}>
      <video
        ref={video}
        src={`/api/media/${recording.id}`}
        autoPlay
        playsInline
        onClick={toggle}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrent(event.currentTarget.currentTime)}
        onDurationChange={(event) => setDuration(event.currentTarget.duration)}
        onLoadedMetadata={(event) => {
          const { videoWidth, videoHeight } = event.currentTarget;
          if (videoWidth > 0 && videoHeight > 0) onAspectRatio?.(videoWidth / videoHeight);
        }}
        onLoadStart={() => setWaiting(true)}
        onCanPlay={() => setWaiting(false)}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
      />
      {waiting && <span className="player-spinner" aria-label="영상 불러오는 중" />}
      {!playing && !waiting && (
        <button className="player-center" onClick={toggle} aria-label="재생">
          <Play fill="currentColor" />
        </button>
      )}
      <div className="player-controls" onDoubleClick={(event) => event.stopPropagation()}>
        <input
          className="player-seek"
          type="range"
          min="0"
          max={duration || 0}
          step="0.1"
          value={Math.min(current, duration || 0)}
          onChange={(event) => seek(Number(event.target.value))}
          style={{ "--progress": `${duration ? (current / duration) * 100 : 0}%` } as React.CSSProperties}
          aria-label="재생 위치"
        />
        <div className="player-buttons">
          <button onClick={toggle} aria-label={playing ? "일시정지" : "재생"}>
            {playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}
          </button>
          <button onClick={toggleMute} aria-label={muted ? "음소거 해제" : "음소거"}>
            {muted || volume === 0 ? <VolumeX /> : <Volume2 />}
          </button>
          <input className="player-volume" type="range" min="0" max="1" step="0.05" value={muted ? 0 : volume} onChange={(event) => changeVolume(Number(event.target.value))} aria-label="볼륨" />
          <span>{mediaTime(current)} / {mediaTime(duration)}</span>
          <button className="player-fullscreen" onClick={fullscreen} aria-label="전체 화면"><Maximize /></button>
        </div>
      </div>
    </div>
  );
}

function PlayerModal({ recording, onClose }: { recording: Recording; onClose: () => void }) {
  const dialog = useRef<HTMLElement>(null);
  const resizing = useRef(false);
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
    const clamp = () => setDialogSize((current) => ({
      width: Math.min(current.width, window.innerWidth - 24),
      height: Math.min(current.height, window.innerHeight - 24),
    }));
    const close = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.body.style.overflow = "hidden";
    window.addEventListener("resize", clamp);
    window.addEventListener("keydown", close);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("resize", clamp);
      window.removeEventListener("keydown", close);
    };
  }, [onClose]);

  const fitDialogToVideo = (requestedPlayerWidth: number, ratio: number, chromeWidth: number, chromeHeight: number) => {
    const maxDialogWidth = Math.max(280, window.innerWidth - 24);
    const maxDialogHeight = Math.max(240, window.innerHeight - 24);
    const minDialogWidth = Math.min(360, maxDialogWidth);
    const minDialogHeight = Math.min(300, maxDialogHeight);
    const maxPlayerWidth = Math.max(1, Math.min(
      maxDialogWidth - chromeWidth,
      (maxDialogHeight - chromeHeight) * ratio,
    ));
    const minPlayerWidth = Math.min(maxPlayerWidth, Math.max(
      1,
      minDialogWidth - chromeWidth,
      (minDialogHeight - chromeHeight) * ratio,
    ));
    const playerWidth = Math.min(maxPlayerWidth, Math.max(minPlayerWidth, requestedPlayerWidth));
    return {
      width: playerWidth + chromeWidth,
      height: playerWidth / ratio + chromeHeight,
    };
  };

  useEffect(() => {
    let frame = 0;
    const applyRatio = () => {
      frame = 0;
      if (resizing.current) return;
      const dialogElement = dialog.current;
      const playerElement = dialogElement?.querySelector<HTMLElement>(".archive-player");
      if (!dialogElement || !playerElement) return;
      const bounds = layoutSize(dialogElement);
      const playerBounds = layoutSize(playerElement);
      const next = fitDialogToVideo(
        playerBounds.width,
        aspectRatio,
        bounds.width - playerBounds.width,
        bounds.height - playerBounds.height,
      );
      setDialogSize((current) => (
        Math.abs(current.width - next.width) < 0.01 && Math.abs(current.height - next.height) < 0.01
          ? current
          : next
      ));
    };
    const scheduleRatio = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(applyRatio);
    };
    const dialogElement = dialog.current;
    const playerElement = dialogElement?.querySelector<HTMLElement>(".archive-player");
    if (!dialogElement || !playerElement) return;
    const observer = new ResizeObserver(scheduleRatio);
    observer.observe(dialogElement);
    observer.observe(playerElement);
    scheduleRatio();
    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [aspectRatio]);

  const enforceAspectRatio = (ratio: number) => {
    if (!Number.isFinite(ratio) || ratio <= 0) return;
    setAspectRatio(ratio);
  };

  const startResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const dialogElement = dialog.current;
    const playerElement = dialogElement?.querySelector<HTMLElement>(".archive-player");
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
      const dominantDelta = Math.abs(horizontalDelta) >= Math.abs(verticalDelta) ? horizontalDelta : verticalDelta;
      setDialogSize(fitDialogToVideo(
        origin.playerWidth + dominantDelta,
        aspectRatio,
        origin.chromeWidth,
        origin.chromeHeight,
      ));
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

  const closeFromBackdrop = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget && !resizing.current) onClose();
  };

  return (
    <motion.div className="player-overlay" onPointerDown={closeFromBackdrop} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
      <motion.section
        ref={dialog}
        className="player-dialog"
        style={{ width: dialogSize.width, height: dialogSize.height }}
        onClick={(event) => event.stopPropagation()}
        initial={{ opacity: 0, scale: 0.97, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.98, y: 8 }}
        transition={{ type: "spring", stiffness: 350, damping: 31 }}
      >
        <div className="player-dialog-head">
          <div><small>{recording.channel} · {typeLabel(recording.type)}</small><strong>{recording.title}</strong></div>
          <button onClick={onClose} aria-label="플레이어 닫기">✕</button>
        </div>
        <div className="player-dialog-media"><ArchivePlayer recording={recording} onAspectRatio={enforceAspectRatio} /></div>
        <div className="player-dialog-foot">{new Date(recording.created_at).toLocaleString("ko-KR")} · {size(recording.size)}</div>
        <button className="player-resize" onPointerDown={startResize} aria-label="플레이어 크기 조절" title="드래그하여 크기 조절" />
      </motion.section>
    </motion.div>
  );
}

function RecordingGrid({ recordings, onPlay, canPurge = false }: {
  recordings: Recording[];
  onPlay: (recording: Recording) => void;
  canPurge?: boolean;
}) {
  const qc = useQueryClient();
  const cancel = async (r: Recording) => {
    if (
      confirm(
        r.type === "live" ? "녹화를 중단할까요?" : "다운로드를 취소할까요?",
      )
    ) {
      await api(`/api/recordings/${r.id}/cancel`, { method: "POST" });
      qc.invalidateQueries({ queryKey: ["recordings"] });
    }
  };
  return (
    <div className="recording-grid">
      {recordings.map((r) => (
        <article key={r.id}>
          <div
            className={`thumb ${r.state === "completed" ? "viewable" : ""}`}
            style={
              r.thumbnail ? { backgroundImage: `url(${r.thumbnail})` } : {}
            }
            onClick={() => r.state === "completed" && onPlay(r)}
            onKeyDown={(event) => {
              if (r.state === "completed" && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                onPlay(r);
              }
            }}
            role={r.state === "completed" ? "button" : undefined}
            tabIndex={r.state === "completed" ? 0 : undefined}
            aria-label={r.state === "completed" ? `${r.title} 재생` : undefined}
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
            {(r.state === "recording" || r.state === "queued" || r.state === "processing") && (
              <div className={`download-progress ${r.type === "live" ? "live-recording-progress" : ""}`}>
                {r.type === "live" && r.state !== "processing" && (
                  <div className="live-recording-status">
                    <div>
                      <span className={r.recording_active && r.speed_bps > 0 ? "healthy" : "connecting"}>
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
                      <span>{size(r.size)} 저장됨{r.speed_bps > 0 ? ` · ${speed(r.speed_bps)}` : ""}</span>
                    </div>
                    <div className={`live-recording-meter ${r.recording_active ? "writing" : ""}`}><i /></div>
                  </div>
                )}
                <div>
                  <span>
                    {r.state === "processing"
                      ? "HEVC 인코딩 중"
                      : r.progress != null
                      ? `${r.progress.toFixed(1)}%`
                      : "연결 중"}
                  </span>
                  <span>
                    {size(r.size)}
                    {r.total_size ? ` / ${size(r.total_size)}` : ""}
                  </span>
                </div>
                <progress max="100" value={r.progress ?? 0} />
                <div className="download-eta">
                  <span>{speed(r.speed_bps)}</span>
                  <span>{eta(r.eta_seconds)}</span>
                </div>
              </div>
            )}
            <p>
              {new Date(r.created_at).toLocaleString("ko-KR")} · {size(r.size)}
            </p>
            {r.error && <p className="error">{r.error}</p>}
            <div className="recording-actions">
              {(r.state === "recording" || r.state === "queued" || r.state === "processing") && (
                <button className="cancel" onClick={() => cancel(r)}>
                  {r.state === "processing" ? "인코딩 취소" : r.type === "live" ? "녹화 중단" : "다운로드 취소"}
                </button>
              )}
              <button
                onClick={async () => {
                  if (confirm("내 라이브러리에서 제거할까요?")) {
                    await api(`/api/recordings/${r.id}`, { method: "DELETE" });
                    qc.invalidateQueries({ queryKey: ["recordings"] });
                  }
                }}
              >
                내 기록에서 제거
              </button>
              {canPurge && !IN_FLIGHT_STATES.has(r.state) && (
                <button
                  className="purge"
                  onClick={async () => {
                    if (confirm(`“${r.title}” 아카이브를 영구 삭제할까요?\n모든 사용자에게서 제거되며 영상 파일도 삭제됩니다.`)) {
                      await api(`/api/admin/recordings/${r.id}`, { method: "DELETE" });
                      qc.invalidateQueries({ queryKey: ["recordings"] });
                      qc.invalidateQueries({ queryKey: ["admin"] });
                    }
                  }}
                >
                  <Trash2 size={13} /> 아카이브 삭제
                </button>
              )}
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}
function SettingsPage() {
  const [code, setCode] = useState("");
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
  const { data: user, error } = useQuery({
    queryKey: ["me"],
    queryFn: () => api<User>("/api/me"),
    retry: false,
    enabled: status && !status.setup_required,
  });
  if (isLoading) return null;
  if (status?.setup_required) return <Auth setup />;
  if (error || !user) return <Auth setup={false} />;
  return <Shell user={user} />;
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: {
              // Poll rapidly only while something is actually in flight, and
              // stop entirely in a background tab. An idle library of hundreds
              // of recordings otherwise re-fetches every second for no reason.
              refetchInterval: (query) => (hasActiveWork(query.state.data) ? 1000 : 15000),
              refetchIntervalInBackground: false,
            },
          },
        })
      }
    >
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
