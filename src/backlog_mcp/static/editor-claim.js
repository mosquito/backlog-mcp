// A reload keeps its edit marker. Other tabs must acquire their own claim.
export function editClaim(task, { call, principal, endpoint = "/mcp" }) {
  const lockKey = `backlog:edit-claim:${endpoint}:${principal}:${task.id}`;
  const tabKey = "backlog:edit-tab";
  let tab = sessionStorage.getItem(tabKey);
  if (!tab) sessionStorage.setItem(tabKey, (tab = crypto.randomUUID()));
  const key = `${lockKey}:${tab}`;
  let lease = null,
    unlock = null,
    lockReady = null,
    closing = null;
  function stored() {
    try {
      return JSON.parse(localStorage.getItem(key));
    } catch {
      return null;
    }
  }
  async function lock() {
    if (!lockReady)
      lockReady = new Promise((resolve, reject) => {
        if (!navigator.locks)
          return reject(new Error("This browser needs HTTPS to protect concurrent editing."));
        navigator.locks
          .request(lockKey, { ifAvailable: true }, async (held) => {
            if (!held) {
              reject(new Error("This task is open in another browser tab."));
              return;
            }
            await new Promise((done) => {
              unlock = done;
              resolve();
            });
          })
          .catch(reject);
      });
    try {
      await lockReady;
    } catch (error) {
      lockReady = null;
      throw error;
    }
  }
  function clear() {
    if (stored()?.token === lease?.token) localStorage.removeItem(key);
    lease = null;
  }
  return {
    get token() {
      return lease?.token;
    },
    async acquire(current) {
      await lock();
      const previous = stored();
      if (previous?.tab === tab && current.assigned === previous.token) {
        lease = previous;
        return current;
      }
      lease = {
        tab,
        token: `${principal}:ui-edit-${crypto.randomUUID()}`,
        previous: current.assigned || null,
      };
      // Persist before sending so an interrupted response can recover on reload.
      localStorage.setItem(key, JSON.stringify(lease));
      try {
        return await call("task_claim", {
          task: task.id,
          expected: lease.previous,
          assigned: lease.token,
          temporary: true,
        });
      } catch (error) {
        const latest = await call("task_get", { id: task.id });
        if (latest.assigned === lease.token) return latest;
        clear();
        throw error;
      }
    },
    saved() {
      clear();
    },
    release() {
      if (closing) return closing;
      closing = (async () => {
        try {
          if (!lease) return;
          const current = await call("task_get", { id: task.id });
          if (current.assigned === lease.token)
            await call("task_claim", {
              task: task.id,
              expected: lease.token,
              assigned: null,
              restore: true,
            });
          clear();
        } finally {
          unlock?.();
        }
      })();
      return closing;
    },
  };
}
