// Card collapse memory. Tape and position toggles keep their own keys.

export function cardOpenKey(id) {
  return `the-book-card-open:${id}`;
}

export function defaultCardOpen(sleeve) {
  const key = String(sleeve || "").trim().toLowerCase();
  return key !== "equities" && key !== "equity";
}

export function modelCardId(model) {
  const sleeve = String(model?.sleeve || "").trim().toLowerCase() || "book";
  const slug = String(model?.name || "model")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `model-${sleeve}-${slug || "model"}`;
}

export function storedOpen(storage, key, fallback) {
  try {
    const value = storage.getItem(key);
    if (value === "closed") return false;
    if (value === "open") return true;
  } catch {
    /* localStorage can throw in private mode. */
  }
  return fallback;
}

export function rememberOpen(storage, key, open) {
  try {
    storage.setItem(key, open ? "open" : "closed");
  } catch {
    /* Ignore quota and private-mode failures. The toggle still works this visit. */
  }
}
