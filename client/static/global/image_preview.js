(() => {
  "use strict";

  const MODAL_ID = "globalImagePreviewModal";

  document.addEventListener("DOMContentLoaded", ensureImagePreviewModal);
  document.addEventListener("click", handleImageClick, true);

  function handleImageClick(event) {
    const image = event.target?.closest?.("img");
    if (!image) return;
    if (image.closest(".brand") || image.closest(".brand-area") || image.closest(".site-brand") || image.hasAttribute("data-no-preview")) return;
    if (image.getAttribute("aria-hidden") === "true") return;

    const source = image.dataset.previewSrc || image.currentSrc || image.src;
    if (!source) return;

    const shouldPreview = image.dataset.imagePreview === "1" || image.classList.contains("previewable-image") || isContentImage(image);
    if (!shouldPreview) return;

    event.preventDefault();
    event.stopPropagation();
    openImagePreview(source, image.alt || image.dataset.previewTitle || "Image preview");
  }

  function isContentImage(image) {
    if (image.closest("button") && !image.dataset.imagePreview) return false;
    return Boolean(
      image.closest(".species-card") ||
      image.closest(".species-full-table") ||
      image.closest(".species-current-image") ||
      image.closest(".team-card") ||
      image.closest(".result-image-wrap") ||
      image.classList.contains("result-image") ||
      image.id === "cameraFeed" ||
      image.dataset.previewable === "true"
    );
  }

  function ensureImagePreviewModal() {
    if (document.getElementById(MODAL_ID)) return;
    const style = document.createElement("style");
    style.textContent = `
      .image-preview-modal::backdrop { background: rgba(4, 16, 24, .72); backdrop-filter: blur(4px); }
      .image-preview-modal { width: min(94vw, 72rem); max-width: 72rem; border: 0; padding: 0; background: transparent; color: inherit; }
      .image-preview-card { overflow: hidden; border-radius: 1.35rem; background: var(--surface, var(--card, #ffffff)); border: 1px solid var(--border, rgba(15,23,42,.16)); box-shadow: 0 24px 80px rgba(0,0,0,.35); }
      .image-preview-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: .85rem 1rem; border-bottom: 1px solid var(--border, rgba(15,23,42,.14)); }
      .image-preview-title { margin: 0; font-size: .94rem; font-weight: 900; color: var(--text, #0f172a); }
      .image-preview-close { width: 2.35rem; height: 2.35rem; border-radius: 999px; border: 1px solid var(--border, rgba(15,23,42,.16)); background: var(--surface-2, rgba(148,163,184,.12)); color: var(--text, #0f172a); cursor: pointer; font-size: 1.35rem; line-height: 1; }
      .image-preview-body { padding: .75rem; background: var(--surface-2, rgba(148,163,184,.10)); }
      .image-preview-img { display: block; width: 100%; max-height: min(78vh, 50rem); object-fit: contain; border-radius: 1rem; background: #061018; }
      @media (max-width: 520px) { .image-preview-modal { width: 96vw; } .image-preview-card { border-radius: 1rem; } .image-preview-head { padding: .7rem .8rem; } .image-preview-body { padding: .5rem; } }
    `;
    document.head.appendChild(style);

    const modal = document.createElement("dialog");
    modal.id = MODAL_ID;
    modal.className = "image-preview-modal";
    modal.innerHTML = `
      <div class="image-preview-card">
        <div class="image-preview-head">
          <p class="image-preview-title" id="globalImagePreviewTitle">Image preview</p>
          <button class="image-preview-close" type="button" aria-label="Close image preview">×</button>
        </div>
        <div class="image-preview-body">
          <img class="image-preview-img" id="globalImagePreviewImage" alt="Image preview">
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    modal.querySelector(".image-preview-close")?.addEventListener("click", () => modal.close());
    modal.addEventListener("click", (event) => {
      if (event.target === modal) modal.close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal.open) modal.close();
    });
  }

  function openImagePreview(source, title) {
    ensureImagePreviewModal();
    const modal = document.getElementById(MODAL_ID);
    const image = document.getElementById("globalImagePreviewImage");
    const titleNode = document.getElementById("globalImagePreviewTitle");
    if (!modal || !image) return;
    image.src = source;
    image.alt = title || "Image preview";
    if (titleNode) titleNode.textContent = title || "Image preview";
    if (typeof modal.showModal === "function") modal.showModal();
  }

  window.AquaCultureImagePreview = { open: openImagePreview };
})();
