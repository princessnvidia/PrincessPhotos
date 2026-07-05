# PrincessPhotos 📸

Modern photo management application for Linux focused on **speed, elegance and large photo libraries**.

PrincessPhotos is an experimental desktop application designed to make browsing, organizing and enjoying your photos effortless while remaining lightweight and fully open source.

---

<p align="center">
  <img src="docs/demo.gif" alt="PrincessPhotos Demo" width="100%">
</p>

---

# Features

## 🖼 Photo Library

- Import photo folders
- Automatic library organization
- Albums
- Favorites
- Recently imported
- Instant search

## 🔍 Photo Viewer

- Fullscreen viewing
- Smooth zoom
- Pan & navigation
- Slideshow mode
- Keyboard shortcuts

## 📂 Organization

- Drag & Drop
- Collections
- Tags
- Ratings
- Duplicate detection *(planned)*

## 📸 Metadata

- EXIF information
- Camera model
- Lens information
- Capture date
- File properties

## ✨ Editing

- Crop
- Rotate
- Flip
- Brightness
- Contrast
- Saturation
- Non-destructive editing *(planned)*

## 🚀 Performance

- Fast thumbnail generation
- Lazy loading
- GPU accelerated rendering
- Large library support

---

# Tech Stack

- Python
- PySide6
- Qt6

---

# Application Architecture

```
Photo Library
      │
      ▼
Thumbnail Cache
      │
      ▼
Photo Viewer
      │
      ▼
Metadata Engine
      │
      ▼
Editing Pipeline
      │
      ▼
Export
```

---

# Roadmap

## Library

- [ ] Timeline view
- [ ] Map view
- [ ] Smart albums
- [ ] Duplicate finder

## Editing

- [ ] RAW support
- [ ] Batch editing
- [ ] Non-destructive editing

## Intelligence

- [ ] Face recognition
- [ ] AI-powered photo search

## Synchronization

- [ ] Cloud synchronization
- [ ] Video support

---

# Installation

```bash
git clone https://github.com/princessnvidia/PrincessPhotos.git
cd PrincessPhotos

pip install -r requirements.txt

python princessphotos.py
```

---

# Philosophy

PrincessPhotos explores what a modern Linux photo manager can be.

The project focuses on fast navigation, clean visual design and efficient organization while avoiding unnecessary complexity. It aims to provide a smooth experience for photographers and everyday users alike, with an interface that stays responsive even when managing large collections.

The long-term vision is to combine elegant desktop workflows with intelligent features such as semantic search, face recognition and non-destructive editing, while remaining fully open source.

---

# Status

🚧 Active Development

---

# License

MIT License
