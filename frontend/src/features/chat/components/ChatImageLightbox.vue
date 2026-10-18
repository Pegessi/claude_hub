<template>
  <Teleport to="body">
    <div
      v-if="imageLightboxUrl"
      ref="lightboxEl"
      class="structured-image-lightbox"
      :class="{ 'structured-image-lightbox--actual': imageLightboxActualSize }"
      role="dialog"
      aria-modal="true"
      aria-label="Image preview"
      @click.self="closeImageLightbox"
    >
      <div class="structured-image-lightbox-toolbar">
        <span v-if="imageLightboxDimensions">{{ imageLightboxDimensions }}</span>
        <button
          type="button"
          :aria-pressed="imageLightboxActualSize"
          @click="imageLightboxActualSize = !imageLightboxActualSize"
        >
          {{ imageLightboxActualSize ? 'Fit image' : 'Actual size' }}
        </button>
        <a
          :href="imageLightboxUrl"
          target="_blank"
          rel="noopener noreferrer"
        >Open original</a>
      </div>
      <button
        ref="lightboxCloseEl"
        type="button"
        class="structured-image-lightbox-close"
        aria-label="Close image preview"
        @click="closeImageLightbox"
      >
        ×
      </button>
      <img
        class="structured-image-lightbox-img"
        :src="imageLightboxUrl"
        :alt="imageLightboxAlt"
        @click.stop
        @load="onLightboxImageLoad"
        @error="closeImageLightbox"
      >
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { requireChatOwner, chatImagesKey } from '../owners'
const { imageLightboxUrl, lightboxEl, imageLightboxActualSize, closeImageLightbox, imageLightboxDimensions, lightboxCloseEl, imageLightboxAlt, onLightboxImageLoad } = requireChatOwner(chatImagesKey)
</script>

<style scoped>
.structured-image-lightbox {
  position: fixed;
  inset: 0;
  z-index: 1600;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 72px 24px 24px;
  background: rgb(0 0 0 / 78%);
  backdrop-filter: blur(4px);
}

.structured-image-lightbox-img {
  max-width: min(1200px, calc(100vw - 48px));
  max-height: calc(100dvh - 96px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
  box-shadow: 0 18px 64px rgb(0 0 0 / 52%);
}

.structured-image-lightbox-toolbar {
  position: fixed;
  z-index: 1;
  top: 16px;
  left: 16px;
  right: 72px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  color: #fff;
  font-size: 12px;
}

.structured-image-lightbox-toolbar button,

.structured-image-lightbox-toolbar a {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  border: 1px solid rgb(255 255 255 / 28%);
  border-radius: var(--ch-radius-sm);
  background: rgb(24 24 24 / 96%);
  color: #fff;
  font: inherit;
  text-decoration: none;
  cursor: pointer;
}

@media (max-width: 480px) {
  .structured-image-lightbox-toolbar > span {
    display: none;
  }
}

.structured-image-lightbox--actual {
  display: block;
  overflow: auto;
}

.structured-image-lightbox--actual .structured-image-lightbox-img {
  display: block;
  max-width: none;
  max-height: none;
  margin: auto;
}

.structured-image-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid rgb(255 255 255 / 28%);
  border-radius: 50%;
  background: rgb(24 24 24 / 88%);
  color: #fff;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}

.structured-image-lightbox-close:hover,

.structured-image-lightbox-close:focus-visible {
  border-color: rgb(255 255 255 / 72%);
  background: rgb(42 42 42 / 96%);
  outline: none;
}
</style>
