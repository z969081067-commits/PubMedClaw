# PubMedClaw: The Ultimate PubMed Search & Download Agent for OpenClaw

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.x-blue.svg)

Tired of wrestling with complex PubMed query strings? Exhausted from endlessly clicking through browser tabs just to download open-access papers? 

**PubMedClaw** is a fully automated literature retrieval skill (Agent) custom-built for OpenClaw. It translates your natural language instructions into precise PubMed searches, intelligently scores and ranks the results across multiple dimensions, and fully automates the extraction of PMC (PubMed Central) open-access PDFs right to your local drive. Let your Agent become your most capable research assistant.

<img width="1024" height="559" alt="image" src="https://github.com/user-attachments/assets/60850042-d6c2-40ab-889e-7499fdf8ec4b" />

## ✨ Key Features

* 🗣️ **Natural Language to Advanced Search:** Say goodbye to tedious `[Title/Abstract] AND ...` syntax. Just tell the agent what you need, and it will automatically extract keywords, expand synonyms, and construct a professional PubMed query.
* 📈 **Intelligent Multi-dimensional Ranking:** Beyond simple keyword matching. The system evaluates topic relevance, article type (with built-in high-weight boosts for *Reviews* and *Meta-analyses*), publication year, and target species to surface the most valuable literature at the very top.
* 📥 **One-Click Full-Text Auto-Archiving:** After searching, simply command "Download paper #X". The script will automatically fetch the PDF from PMC, create a topic-specific folder on your Desktop, and save the files silently in the background.
* ⚡ **Out-of-the-box & Zero Config:** No need to jump through hoops applying for NCBI API Keys. It utilizes public endpoints and works right out of the box.

## 🛠️ Installation Guide

This skill is currently designed for Windows (win32) environments and relies on native PowerShell and Python.

### 1. Prerequisites
Ensure Python is installed on your machine (running `python` or `py` in your terminal should work normally).

### 2. Install Dependencies
Open your terminal, navigate to the `scripts` directory of this Skill, and install the required HTTP networking library:

```powershell
python -m pip install -r "scripts\requirements.txt"
```

### 3. Load into OpenClaw
Drop the entire `PubMedClaw` folder into your OpenClaw skills directory (e.g., `workspace/skills/` or globally at `~/.openclaw/skills/`). Restart or refresh OpenClaw, and the `pubmed_paper_finder` skill will be automatically activated.

## 💬 Usage & Examples

Inside the OpenClaw chat interface, you can assign tasks to it just like you would to a research intern:

### Scenario 1: Precision Literature Search
> *"Find review papers on the application of bioinformatics in cancer research from the last 5 years. Sort by relevance and give me the top 5 best matches."*

The agent will quickly return a numbered list of papers, including the DOI, journal, publication year, and a brief explanation of why each paper is a good fit for your request (e.g., matched the "Review" type, falls within the timeframe).

### Scenario 2: Fully Automated Download
> *"Download the 1st and 3rd papers from that list to my computer."*

The agent will create a topic folder on your Desktop and neatly save the PDF full texts inside. If it encounters a non-open-access (Paywalled) paper, it will clearly report the failure reason and provide the original link for manual retrieval.

4. **徽章预置：** 在最上方直接帮你把 MIT License 和 Python 的徽章代码写好了，你复制粘贴到 GitHub 后会直接显示出漂亮的图标。

你可以直接在你的 GitHub 仓库里点击 `Add file -> Create new file`，命名为 `README_EN.md`，然后把上面的内容粘贴进去！如果有哪里觉得用词不习惯，随时告诉我调整。
