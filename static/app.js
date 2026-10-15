document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
    const search = form.querySelector('input[type="search"]');
    let searchTimer;

    if (search) {
      search.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => form.requestSubmit(), 200);
      });
    }
    form.addEventListener("submit", () => window.clearTimeout(searchTimer));
    form.addEventListener("change", (event) => {
      if (event.target.matches("input, select") && event.target !== search) {
        form.requestSubmit();
      }
    });
  });

  const results = document.querySelector("#episode-results");
  const loader = document.querySelector("#infinite-scroll");
  const loadMore = document.querySelector("#load-more");
  const status = document.querySelector("#infinite-scroll-status");
  const progress = document.querySelector("#result-progress");
  if (!results || !loader || !status) {
    return;
  }

  const restorationKey = `episode-scroll:${window.location.pathname}${window.location.search}`;
  let loading = false;
  let observer;

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      window.sessionStorage.removeItem(restorationKey);
    }
  });

  const updateLoadedCount = (additional) => {
    const loaded = Number(results.dataset.loaded || 0) + Number(additional || 0);
    results.dataset.loaded = String(loaded);
    if (progress) {
      progress.textContent = `Showing ${loaded} of ${results.dataset.total}`;
    }
    return loaded;
  };

  const loadNextBatch = async () => {
    const nextUrl = loader.dataset.nextFragmentUrl;
    if (!nextUrl || loading) {
      return false;
    }

    loading = true;
    loader.setAttribute("aria-busy", "true");
    if (loadMore) {
      loadMore.setAttribute("aria-disabled", "true");
      loadMore.textContent = "Loading…";
    }
    status.textContent = "Loading more titles…";
    try {
      const response = await fetch(nextUrl, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(`Request failed with status ${response.status}`);
      }
      const payload = await response.json();
      const fragment = document.createElement("template");
      fragment.innerHTML = payload.html.trim();
      results.append(fragment.content);
      const loaded = updateLoadedCount(payload.loaded);

      if (payload.next_fragment_url) {
        loader.dataset.nextFragmentUrl = payload.next_fragment_url;
        if (loadMore) {
          loadMore.href = payload.next_url;
          loadMore.textContent = "Load more";
        }
        status.textContent = `${payload.loaded} more titles loaded.`;
      } else {
        delete loader.dataset.nextFragmentUrl;
        if (loadMore) {
          loadMore.remove();
        }
        status.textContent = `All ${loaded} titles loaded.`;
        observer?.disconnect();
      }
      return true;
    } catch (_error) {
      status.textContent = "Loading failed. Select Load more to retry.";
      if (loadMore) {
        loadMore.textContent = "Load more";
      }
      observer?.unobserve(loader);
      return false;
    } finally {
      loading = false;
      loader.removeAttribute("aria-busy");
      loadMore?.removeAttribute("aria-disabled");
    }
  };

  loadMore?.addEventListener("click", async (event) => {
    event.preventDefault();
    if (await loadNextBatch()) {
      observer?.observe(loader);
    }
  });

  results.addEventListener("click", (event) => {
    const link = event.target.closest(".episode-card a");
    if (!link) {
      return;
    }
    try {
      window.sessionStorage.setItem(restorationKey, JSON.stringify({
        scrollY: window.scrollY,
        loaded: Number(results.dataset.loaded || 0),
      }));
    } catch (_error) {
      // Browsing remains functional if session storage is unavailable.
    }
  });

  const restoreScrollPosition = async () => {
    let saved;
    try {
      saved = JSON.parse(window.sessionStorage.getItem(restorationKey) || "null");
      window.sessionStorage.removeItem(restorationKey);
    } catch (_error) {
      saved = null;
    }
    if (!saved) {
      return;
    }
    while (
      Number(results.dataset.loaded || 0) < Number(saved.loaded || 0)
      && loader.dataset.nextFragmentUrl
    ) {
      if (!(await loadNextBatch())) {
        break;
      }
    }
    window.requestAnimationFrame(() => window.scrollTo(0, Number(saved.scrollY || 0)));
  };

  const startInfiniteScroll = async () => {
    await restoreScrollPosition();
    if (!("IntersectionObserver" in window) || !loader.dataset.nextFragmentUrl) {
      return;
    }
    observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        loadNextBatch();
      }
    }, { rootMargin: "800px 0px" });
    observer.observe(loader);
  };

  startInfiniteScroll();
});
