type MessageHandler<T> = (data: T) => void;
type StatusHandler = (status: 'connecting' | 'connected' | 'disconnected') => void;

export interface WsHandle {
  close: () => void;
}

export function createWsConnection<T>(
  path: string,
  onMessage: MessageHandler<T>,
  onStatus: StatusHandler,
): WsHandle {
  let ws: WebSocket | null = null;
  let delay = 1000;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (stopped) return;
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${protocol}://${location.host}${path}`;
    onStatus('connecting');
    ws = new WebSocket(url);

    ws.onopen = () => {
      delay = 1000;
      onStatus('connected');
    };

    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data as string) as T);
      } catch {
        // ignore malformed
      }
    };

    ws.onclose = () => {
      if (stopped) return;
      onStatus('disconnected');
      timer = setTimeout(connect, delay);
      delay = Math.min(delay * 2, 8000);
    };

    ws.onerror = () => {
      ws?.close();
    };
  }

  connect();

  return {
    close() {
      stopped = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    },
  };
}
