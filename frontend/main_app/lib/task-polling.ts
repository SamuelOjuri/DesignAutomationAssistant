type PollingEnvironment = {
  isVisible: () => boolean;
  subscribe: (listener: () => void) => () => void;
  schedule: (listener: () => void, milliseconds: number) => ReturnType<typeof setTimeout>;
  cancel: (timer: ReturnType<typeof setTimeout>) => void;
};

type TaskPollingState = { syncStatus?: string | null };

export function taskPollingInterval(state: TaskPollingState | void): number {
  return state?.syncStatus === "completed" || state?.syncStatus === "failed" ? 60_000 : 12_000;
}

/** Keep discovering server changes after ingestion completes, without overlapping reads. */
export function startTaskPolling(
  refresh: (signal: AbortSignal, first: boolean) => Promise<TaskPollingState | void>,
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
  let interval = 12_000;
  const poll = async () => {
    if (busy || controller.signal.aborted) return;
    if (timer !== undefined) environment.cancel(timer);
    if (environment.isVisible()) {
      busy = true;
      try {
        interval = taskPollingInterval(await refresh(controller.signal, first));
        first = false;
      } catch (error) {
        interval = 12_000;
        if (!controller.signal.aborted) onError(error);
      } finally {
        busy = false;
      }
    }
    if (!controller.signal.aborted) timer = environment.schedule(() => void poll(), interval);
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
