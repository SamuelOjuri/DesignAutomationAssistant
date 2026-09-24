type PollingEnvironment = {
  isVisible: () => boolean;
  subscribe: (listener: () => void) => () => void;
  schedule: (listener: () => void, milliseconds: number) => ReturnType<typeof setTimeout>;
  cancel: (timer: ReturnType<typeof setTimeout>) => void;
};

/** Keep discovering server changes after ingestion completes, without overlapping reads. */
export function startTaskPolling(
  refresh: (signal: AbortSignal, first: boolean) => Promise<void>,
  onError: (error: unknown) => void,
  environment: PollingEnvironment = {
    isVisible: () => document.visibilityState === "visible",
    subscribe: (listener) => {
      window.addEventListener("focus", listener);
      document.addEventListener("visibilitychange", listener);
      return () => {
        window.removeEventListener("focus", listener);
        document.removeEventListener("visibilitychange", listener);
      };
    },
    schedule: (listener, milliseconds) => setTimeout(listener, milliseconds),
    cancel: (timer) => clearTimeout(timer),
  },
) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let busy = false;
  let first = true;
  const poll = async () => {
    if (busy || controller.signal.aborted) return;
    if (timer !== undefined) environment.cancel(timer);
    if (environment.isVisible()) {
      busy = true;
      try {
        await refresh(controller.signal, first);
        first = false;
      } catch (error) {
        if (!controller.signal.aborted) onError(error);
      } finally {
        busy = false;
      }
    }
    if (!controller.signal.aborted) timer = environment.schedule(() => void poll(), 12_000);
  };
  const unsubscribe = environment.subscribe(() => {
    if (environment.isVisible()) void poll();
  });
  void poll();
  return () => {
    controller.abort();
    if (timer !== undefined) environment.cancel(timer);
    unsubscribe();
  };
}
