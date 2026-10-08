// Event delegation on the whole grid -- works for every card, added or existing.
const grid = document.querySelector(".grid");

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

function updateStatCount(id, delta) {
  const el = document.getElementById(id);
  if (el) {
    const cur = parseInt(el.textContent, 10);
    if (!isNaN(cur)) {
      el.textContent = Math.max(0, cur + delta);
    }
  }
}

grid.addEventListener("change", async (e) => {
  if (!e.target.classList.contains("mark-checkbox")) return;
  const card = e.target.closest(".card");
  const id = card.dataset.photoId;
  try {
    const data = await api(`/api/photo/${id}/toggle-removal`, { method: "POST" });
    card.classList.toggle("marked", data.marked_for_removal);
    updateStatCount("stat-count-marked", data.marked_for_removal ? 1 : -1);
  } catch (err) {
    alert("Could not update: " + err.message);
    e.target.checked = !e.target.checked; // revert on failure
  }
});

function syncQuickTagButton(card, tagName, isActive) {
  const btn = card.querySelector(`.quick-tag-btn[data-tag="${tagName}"]`);
  if (btn) {
    btn.classList.toggle("active", isActive);
    btn.innerHTML = `${isActive ? "&check;" : "+"} ${tagName}`;
    btn.title = `${isActive ? "Remove" : "Add"} '${tagName}' tag`;
  }
}

grid.addEventListener("click", async (e) => {
  const quickBtn = e.target.closest(".quick-tag-btn");
  if (quickBtn) {
    const card = quickBtn.closest(".card");
    const id = card.dataset.photoId;
    const tagName = quickBtn.dataset.tag;
    const existingTagSpan = card.querySelector(`.tags .tag[data-tag-name="${tagName}"]`);

    quickBtn.disabled = true;
    try {
      if (existingTagSpan) {
        const tagId = existingTagSpan.dataset.tagId;
        await api(`/api/photo/${id}/tags/${tagId}`, { method: "DELETE" });
        existingTagSpan.remove();
        syncQuickTagButton(card, tagName, false);
        if (tagName === "4boys") updateStatCount("stat-count-4boys", -1);
      } else {
        const tag = await api(`/api/photo/${id}/tags`, {
          method: "POST",
          body: JSON.stringify({ name: tagName }),
        });
        const tagsContainer = card.querySelector(".tags");
        const addInput = tagsContainer.querySelector(".add-tag-input");
        const span = document.createElement("span");
        span.className = "tag";
        span.dataset.tagId = tag.id;
        span.dataset.tagName = tag.name;
        span.innerHTML = `${tag.name}<button class="tag-remove" data-tag-id="${tag.id}" title="Remove tag">&times;</button>`;
        tagsContainer.insertBefore(span, addInput);
        card.querySelector(".untagged-badge")?.remove();
        syncQuickTagButton(card, tagName, true);
        if (tagName === "4boys") updateStatCount("stat-count-4boys", 1);
      }
    } catch (err) {
      alert("Could not update tag: " + err.message);
    } finally {
      quickBtn.disabled = false;
    }
    return;
  }

  if (e.target.classList.contains("tag-remove")) {
    const card = e.target.closest(".card");
    const id = card.dataset.photoId;
    const tagId = e.target.dataset.tagId;
    const tagSpan = e.target.closest(".tag");
    const tagName = tagSpan ? (tagSpan.dataset.tagName || tagSpan.childNodes[0]?.textContent?.trim().toLowerCase()) : null;
    try {
      await api(`/api/photo/${id}/tags/${tagId}`, { method: "DELETE" });
      tagSpan.remove();
      if (tagName) {
        syncQuickTagButton(card, tagName, false);
        if (tagName === "4boys") updateStatCount("stat-count-4boys", -1);
      }
    } catch (err) {
      alert("Could not remove tag: " + err.message);
    }
  }

  if (e.target.classList.contains("save-desc")) {
    const card = e.target.closest(".card");
    const id = card.dataset.photoId;
    const textarea = card.querySelector(".description");
    const flash = card.querySelector(".saved-flash");
    try {
      await api(`/api/photo/${id}/description`, {
        method: "POST",
        body: JSON.stringify({ description: textarea.value }),
      });
      flash.textContent = "Saved";
      flash.classList.add("show");
      setTimeout(() => flash.classList.remove("show"), 1200);
    } catch (err) {
      alert("Could not save description: " + err.message);
    }
  }
});

grid.addEventListener("keydown", async (e) => {
  if (!e.target.classList.contains("add-tag-input") || e.key !== "Enter") return;
  e.preventDefault();
  const input = e.target;
  const name = input.value.trim().toLowerCase();
  if (!name) return;
  const card = input.closest(".card");
  const id = card.dataset.photoId;
  try {
    const tag = await api(`/api/photo/${id}/tags`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    let span = card.querySelector(`.tags .tag[data-tag-name="${tag.name}"]`);
    if (!span) {
      span = document.createElement("span");
      span.className = "tag";
      span.dataset.tagId = tag.id;
      span.dataset.tagName = tag.name;
      span.innerHTML = `${tag.name}<button class="tag-remove" data-tag-id="${tag.id}" title="Remove tag">&times;</button>`;
      input.parentElement.insertBefore(span, input);
      if (tag.name === "4boys") updateStatCount("stat-count-4boys", 1);
    }
    input.value = "";
    card.querySelector(".untagged-badge")?.remove();
    syncQuickTagButton(card, tag.name, true);
  } catch (err) {
    alert("Could not add tag: " + err.message);
  }
});

