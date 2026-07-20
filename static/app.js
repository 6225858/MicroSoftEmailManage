const AUTO_REFRESH_INTERVAL = 15000;
const NO_TAG_FILTER_VALUE = "__NO_TAG__";
const MOBILE_LAYOUT_BREAKPOINT = 980;
const DEFAULT_LOG_PAGE_SIZE = 10;

// ───── 自定义下拉组件（替代浏览器原生 <select>）─────
// 用法：enhanceSelect(document.getElementById("xxx"))
// 包装后的下拉：保留原生 <select> 用于表单提交，但显示用自定义下拉面板
// 自定义下拉会同步 select.value，并触发原生 change 事件
const _cselectInstances = new WeakMap();

function enhanceSelect(selectEl) {
    if (!selectEl || _cselectInstances.has(selectEl)) return null;
    if (selectEl.dataset.enhanced === "true") return _cselectInstances.get(selectEl);
    selectEl.dataset.enhanced = "true";

    // 给原生 select 加上 hidden 类（视觉上隐藏但 DOM 仍存在，change 事件仍可触发）
    selectEl.classList.add("select-hidden");

    // 构造包装
    const wrapper = document.createElement("div");
    wrapper.className = "cselect";

    // 触发按钮
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cselect-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.innerHTML = `
        <span class="cselect-trigger-text"></span>
        <svg class="cselect-arrow" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M5 7.5L10 12.5L15 7.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
    `;

    // 下拉面板
    const panel = document.createElement("div");
    panel.className = "cselect-panel";
    panel.setAttribute("role", "listbox");

    wrapper.appendChild(trigger);
    wrapper.appendChild(panel);
    selectEl.parentNode.insertBefore(wrapper, selectEl);
    wrapper.appendChild(selectEl);

    // 当前选中项的显示文本
    function updateTriggerText() {
        const opt = selectEl.options[selectEl.selectedIndex];
        trigger.querySelector(".cselect-trigger-text").textContent = opt ? opt.textContent : "";
    }

    // 重新渲染选项
    function rebuildOptions() {
        panel.innerHTML = "";
        for (const opt of Array.from(selectEl.options)) {
            const optionEl = document.createElement("div");
            optionEl.className = "cselect-option";
            optionEl.setAttribute("role", "option");
            optionEl.dataset.value = opt.value;
            if (opt.disabled) {
                optionEl.style.opacity = "0.5";
                optionEl.style.pointerEvents = "none";
            }
            optionEl.innerHTML = `
                <span class="cselect-option-text">${escapeHtml(opt.textContent)}</span>
                <svg class="cselect-option-check" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M4.5 10.5L8 14L15.5 6.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
            `;
            if (opt.value === selectEl.value) {
                optionEl.classList.add("is-active");
            }
            optionEl.addEventListener("click", (e) => {
                e.stopPropagation();
                if (opt.disabled) return;
                selectEl.value = opt.value;
                updateTriggerText();
                // 高亮新选项
                panel.querySelectorAll(".cselect-option").forEach((el) => el.classList.remove("is-active"));
                optionEl.classList.add("is-active");
                // 触发原生 change 事件
                selectEl.dispatchEvent(new Event("change", { bubbles: true }));
                closePanel();
            });
            panel.appendChild(optionEl);
        }
        updateTriggerText();
    }

    function openPanel() {
        wrapper.classList.add("is-open");
        trigger.setAttribute("aria-expanded", "true");
    }

    function closePanel() {
        wrapper.classList.remove("is-open");
        trigger.setAttribute("aria-expanded", "false");
    }

    function togglePanel() {
        if (wrapper.classList.contains("is-open")) closePanel();
        else openPanel();
    }

    trigger.addEventListener("click", (e) => {
        e.stopPropagation();
        togglePanel();
    });

    // 点击外部关闭
    document.addEventListener("click", (e) => {
        if (!wrapper.contains(e.target)) closePanel();
    });

    // ESC 关闭
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && wrapper.classList.contains("is-open")) {
            closePanel();
            trigger.focus();
        }
    });

    // 监听原生 <select> 的变化（外部代码修改 value 时同步 UI）
    // 监听原生 select 的变化:
    // - childList 变化(如 updateTagFilterOptions 替换了 options)→ 重建整个面板选项
    // - attributes 变化(如 selected)→ 仅更新 trigger 文字和 active 高亮
    const observer = new MutationObserver((mutations) => {
        const hasStructuralChange = mutations.some((m) => m.type === "childList");
        if (hasStructuralChange) {
            rebuildOptions();
            return;
        }
        updateTriggerText();
        panel.querySelectorAll(".cselect-option").forEach((el) => {
            el.classList.toggle("is-active", el.dataset.value === selectEl.value);
        });
    });
    observer.observe(selectEl, { childList: true, attributes: true });

    // 初始化
    rebuildOptions();

    const instance = { wrapper, trigger, panel, rebuildOptions, openPanel, closePanel };
    _cselectInstances.set(selectEl, instance);
    return instance;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
}

const state = {
    accounts: [],
    selectedAccountId: null,
    tagModalAccountId: null,
    tagModalReturnFocus: null,
    remarkModalAccountId: null,
    remarkModalReturnFocus: null,
    selectedFolder: "inbox",
    mails: [],
    selectedMailId: null,
    loadedMailAccountId: null,
    loadedMailFolder: null,
    mailLoadRequestId: 0,
    mailListEmptyText: "请选择一个邮箱账号后查看邮件",
    mailDetailEmptyText: "请选择一封邮件查看内容",
    autoRefreshEnabled: localStorage.getItem("mail_manager_auto_refresh") === "1",
    accountRefreshTimer: null,
    mailRefreshTimer: null,
    mailRefreshing: false,
    tokenRefreshLogs: [],
    logsPagination: {
        page: 1,
        pageSize: DEFAULT_LOG_PAGE_SIZE,
        total: 0,
        totalPages: 1
    },
    tokenRefreshTaskRunning: false,
    currentView: "accounts",
    mobileMailStep: "accounts",
    apiKeys: [],
    proxies: [],
    // 批量选中邮箱账号(两个邮箱列表共享同一份选中状态)
    selectedAccountIds: new Set(),
    // 批量刷新进行中标志(避免重复触发)
    bulkRefreshing: false
};

const elements = {
    accountCount: document.getElementById("account-count"),
    accountFilterSummary: document.getElementById("account-filter-summary"),
    accountList: document.getElementById("account-list"),
    accountMessage: document.getElementById("account-message"),
    autoRefreshState: document.getElementById("auto-refresh-state"),
    copyMailAddressBtn: document.getElementById("copy-mail-address-btn"),
    exportAccountsBtn: document.getElementById("export-accounts-btn"),
    importBtn: document.getElementById("import-btn"),
    importInput: document.getElementById("import-input"),
    importFileInput: document.getElementById("import-file-input"),
    importMessage: document.getElementById("import-message"),
    importPanel: document.getElementById("import-panel"),
    importProtocolSelect: document.getElementById("import-protocol-select"),
    importServerInput: document.getElementById("import-server-input"),
    importSslSelect: document.getElementById("import-ssl-select"),
    importServerRow: document.getElementById("import-server-row"),
    importSslRow: document.getElementById("import-ssl-row"),
    mailAccountList: document.getElementById("mail-account-list"),
    mailDetail: document.getElementById("mail-detail"),
    mailFilterSummary: document.getElementById("mail-filter-summary"),
    mailList: document.getElementById("mail-list"),
    mailMessage: document.getElementById("mail-message"),
    mailPanelTitle: document.getElementById("mail-panel-title"),
    mailSearchInput: document.getElementById("mail-search-input"),
    mailStepButtons: Array.from(document.querySelectorAll("[data-mail-step-target]")),
    mailStepPanels: Array.from(document.querySelectorAll("[data-mail-step]")),
    mailTagFilter: document.getElementById("mail-tag-filter"),
    menuItems: Array.from(document.querySelectorAll(".menu-item")),
    mobileMailSteps: document.querySelector(".mobile-mail-steps"),
    openMailsBtn: document.getElementById("open-mails-btn"),
    refreshAccountsBtn: document.getElementById("refresh-accounts-btn"),
    refreshLogList: document.getElementById("refresh-log-list"),
    refreshLogMessage: document.getElementById("refresh-log-message"),
    refreshLogPage: document.getElementById("refresh-log-page"),
    refreshLogPaginationSummary: document.getElementById("refresh-log-pagination-summary"),
    refreshLogPageSize: document.getElementById("refresh-log-page-size"),
    refreshLogRunBtn: document.getElementById("refresh-log-run-btn"),
    // 设置页面
    settingsCurrentVersion: document.getElementById("settings-current-version"),
    settingsGithubRepoInput: document.getElementById("settings-github-repo-input"),
    settingsSaveRepoBtn: document.getElementById("settings-save-repo-btn"),
    settingsRepoMessage: document.getElementById("settings-repo-message"),
    settingsCheckUpdateBtn: document.getElementById("settings-check-update-btn"),
    settingsCheckStatus: document.getElementById("settings-check-status"),
    settingsUpdateResult: document.getElementById("settings-update-result"),
    settingsLatestVersion: document.getElementById("settings-latest-version"),
    settingsUpdateBadge: document.getElementById("settings-update-badge"),
    settingsPublishedAt: document.getElementById("settings-published-at"),
    settingsReleaseNotesCard: document.getElementById("settings-release-notes-card"),
    settingsReleaseNotes: document.getElementById("settings-release-notes"),
    settingsReleaseLink: document.getElementById("settings-release-link"),
    settingsUpdateMessage: document.getElementById("settings-update-message"),
    settingsPerformUpdateBtn: document.getElementById("settings-perform-update-btn"),
    // 更新进度弹窗
    updateProgressModal: document.getElementById("update-progress-modal"),
    updateProgressBackdrop: document.getElementById("update-progress-backdrop"),
    updateProgressCloseBtn: document.getElementById("update-progress-close-btn"),
    updateProgressCancelBtn: document.getElementById("update-progress-cancel-btn"),
    updateProgressTitle: document.getElementById("update-progress-title"),
    updateProgressSubtitle: document.getElementById("update-progress-subtitle"),
    updateProgressFill: document.getElementById("update-progress-fill"),
    updateProgressPercent: document.getElementById("update-progress-percent"),
    updateProgressStage: document.getElementById("update-progress-stage"),
    updateVersionInfo: document.getElementById("update-version-info"),
    updateVersionChange: document.getElementById("update-version-change"),
    updateSkippedFiles: document.getElementById("update-skipped-files"),
    updateSkippedFilesList: document.getElementById("update-skipped-files-list"),
    updateErrorBox: document.getElementById("update-error-box"),
    updateErrorMessage: document.getElementById("update-error-message"),
    updateErrorSuggestion: document.getElementById("update-error-suggestion"),
    updateSuccessBox: document.getElementById("update-success-box"),
    updateSuccessMessage: document.getElementById("update-success-message"),
    remarkInput: document.getElementById("remark-input"),
    saveTagsBtn: document.getElementById("save-tags-btn"),
    saveRemarkBtn: document.getElementById("save-remark-btn"),
    searchInput: document.getElementById("search-input"),
    tagFilter: document.getElementById("tag-filter"),
    selectedRemark: document.getElementById("selected-remark"),
    selectedEmail: document.getElementById("selected-email"),
    remarkModal: document.getElementById("remark-modal"),
    remarkModalBackdrop: document.getElementById("remark-modal-backdrop"),
    remarkModalCancelBtn: document.getElementById("remark-modal-cancel-btn"),
    remarkModalCloseBtn: document.getElementById("remark-modal-close-btn"),
    remarkModalCurrent: document.getElementById("remark-modal-current"),
    remarkModalEmail: document.getElementById("remark-modal-email"),
    remarkModalInput: document.getElementById("remark-modal-input"),
    remarkModalMessage: document.getElementById("remark-modal-message"),
    remarkModalSaveBtn: document.getElementById("remark-modal-save-btn"),
    tagModal: document.getElementById("tag-modal"),
    tagModalBackdrop: document.getElementById("tag-modal-backdrop"),
    tagModalCancelBtn: document.getElementById("tag-modal-cancel-btn"),
    tagModalCloseBtn: document.getElementById("tag-modal-close-btn"),
    tagModalCurrentTags: document.getElementById("tag-modal-current-tags"),
    tagModalEmail: document.getElementById("tag-modal-email"),
    tagModalInput: document.getElementById("tag-modal-input"),
    tagModalMessage: document.getElementById("tag-modal-message"),
    tagModalPreview: document.getElementById("tag-modal-preview"),
    tagModalSuggestions: document.getElementById("tag-modal-suggestions"),
    tagModalSaveBtn: document.getElementById("tag-modal-save-btn"),
    tabs: Array.from(document.querySelectorAll(".tab")),
    tagsInput: document.getElementById("tags-input"),
    mailAccountRemark: document.getElementById("mail-account-remark"),
    toggleAutoRefreshBtn: document.getElementById("toggle-auto-refresh-btn"),
    forceRefreshMailsBtn: document.getElementById("force-refresh-mails-btn"),
    viewPanels: Array.from(document.querySelectorAll(".view-panel")),
    viewTitle: document.getElementById("view-title"),
    apiKeyNameInput: document.getElementById("api-key-name-input"),
    apiKeyCreateBtn: document.getElementById("api-key-create-btn"),
    apiKeyList: document.getElementById("api-key-list"),
    apiMessage: document.getElementById("api-message"),
    proxyCount: document.getElementById("proxy-count"),
    proxyAvailableCount: document.getElementById("proxy-available-count"),
    proxyList: document.getElementById("proxy-list"),
    proxyMessage: document.getElementById("proxy-message"),
    proxyImportInput: document.getElementById("proxy-import-input"),
    proxyImportBtn: document.getElementById("proxy-import-btn"),
    proxyImportMessage: document.getElementById("proxy-import-message"),
    proxyCheckBtn: document.getElementById("proxy-check-btn"),
    proxyAddBtn: document.getElementById("proxy-add-btn"),
    proxyAddForm: document.getElementById("proxy-add-form"),
    proxyAddType: document.getElementById("proxy-add-type"),
    proxyAddHost: document.getElementById("proxy-add-host"),
    proxyAddPort: document.getElementById("proxy-add-port"),
    proxyAddUser: document.getElementById("proxy-add-user"),
    proxyAddPass: document.getElementById("proxy-add-pass"),
    proxyAddSaveBtn: document.getElementById("proxy-add-save-btn"),
    proxyAddCancelBtn: document.getElementById("proxy-add-cancel-btn"),
    // 批量删除相关元素
    accountsBulkSelectAll: document.getElementById("accounts-bulk-select-all"),
    accountsBulkDeleteBtn: document.getElementById("accounts-bulk-delete-btn"),
    accountsBulkRefreshBtn: document.getElementById("accounts-bulk-refresh-btn"),
    mailsBulkSelectAll: document.getElementById("mails-bulk-select-all"),
    mailsBulkDeleteBtn: document.getElementById("mails-bulk-delete-btn"),
    mailsBulkRefreshBtn: document.getElementById("mails-bulk-refresh-btn"),
    deleteAccountBtn: document.getElementById("delete-account-btn"),
    deleteConfirmModal: document.getElementById("delete-confirm-modal"),
    deleteConfirmBackdrop: document.getElementById("delete-confirm-backdrop"),
    deleteConfirmCloseBtn: document.getElementById("delete-confirm-close-btn"),
    deleteConfirmCancelBtn: document.getElementById("delete-confirm-cancel-btn"),
    deleteConfirmSaveBtn: document.getElementById("delete-confirm-save-btn"),
    deleteConfirmCount: document.getElementById("delete-confirm-count"),
    deleteConfirmList: document.getElementById("delete-confirm-list"),
    deleteConfirmMessage: document.getElementById("delete-confirm-message")
};

const text = {
    accountsTitle: "账号管理",
    mailsTitle: "邮件查看",
    noMatchingAccounts: "暂无符合条件的邮箱账号",
    noTags: "暂无标签",
    accountCount: (count) => `${count} 个账号`,
    visibleAccountCount: (visible, total) => `当前显示 ${visible} / 总计 ${total} 个邮箱`,
    chooseMailList: "请选择一个邮箱账号后查看邮件",
    emptyMailList: "当前文件夹暂无邮件",
    emptyMailDetail: "请选择一封邮件查看内容",
    noSubject: "无主题",
    noContent: "暂无内容",
    requestFailed: "请求失败",
    autoRefreshOff: "未开启自动刷新",
    autoRefreshOn: "已开启 15 秒自动刷新",
    enableAutoRefresh: "开启自动刷新",
    disableAutoRefresh: "关闭自动刷新",
    autoRefreshEnabledHint: "已开启自动刷新",
    autoRefreshDisabledHint: "已关闭自动刷新",
    loadingMails: "正在加载邮件...",
    loadedMails: (count) => `已加载 ${count} 封邮件`,
    selectedAccountHint: "已选中账号，可以维护标签或查看邮件",
    chooseAccountFirst: "请先选择邮箱账号",
    importInputRequired: "请先粘贴导入内容",
    importFinished: (data) => `导入完成：新增 ${data.inserted}，更新 ${data.updated}，跳过 ${data.skipped}`,
    exportingAccounts: "正在导出账号...",
    exportAccountsSuccess: "账号导出成功，文件已开始下载",
    saveTagsSuccess: "标签保存成功",
    promptTagsTitle: "请输入标签，多个标签请用逗号分隔",
    tagPreviewEmpty: "输入后会在这里实时预览标签效果",
    tagCurrentEmpty: "当前还没有标签",
    savingTags: "保存中...",
    copyMailAddressSuccess: "邮箱地址已复制",
    copyMailAddressUnavailable: "当前没有可复制的邮箱地址",
    bulkDeleteNone: "请先勾选要删除的邮箱账号",
    bulkDeleteConfirmTitle: (count) => `确认删除 ${count} 个邮箱账号?`,
    bulkDeleteSuccess: (count) => `已成功删除 ${count} 个邮箱账号`,
    bulkDeletePartial: (deleted, missing) => `已删除 ${deleted} 个账号${missing ? `，其中 ${missing} 个不存在或已删除` : ""}`,
    deleteSingleConfirm: "确定要删除此邮箱账号吗？此操作不可恢复",
    deleteSingleSuccess: "邮箱账号已删除",
    deletingAccounts: "正在删除...",
    bulkRefreshNone: "请先勾选要刷新邮件的邮箱账号",
    bulkRefreshTriggered: (count, skipped, folder) => `已触发 ${count} 个账号的 ${folder === "junk" ? "垃圾箱" : "收件箱"} 后台刷新${skipped ? `，其中 ${skipped} 个正在刷新中已跳过` : ""}`,
    bulkRefreshing: "刷新中...",
    // 设置页面
    settingsTitle: "设置",
    settingsRepoSaved: "GitHub 仓库地址已保存",
    settingsChecking: "正在检查更新…",
    settingsCheckFailed: (msg) => `检查更新失败: ${msg}`,
    settingsUpToDate: (version) => `当前已是最新版本 (${version})`,
    settingsHasUpdate: (version) => `发现新版本 ${version}，请前往 GitHub 下载更新`,
    settingsNoRepo: "请先在左侧填写并保存 GitHub 仓库地址"
};

const EMPTY_REMARK_TEXT = "暂无备注";
const SAVE_REMARK_SUCCESS_TEXT = "备注保存成功";
const LOG_PAGE_SIZE_OPTIONS = [10, 30, 50];

Object.assign(text, {
    logsTitle: "刷新日志",
    refreshLogsRun: "手动触发任务",
    refreshLogsRunning: "任务执行中...",
    refreshLogsEmpty: "暂无刷新日志",
    refreshLogsLoaded: (total) => `共 ${total} 条日志`,
    refreshLogsTriggerSuccess: (success, total) => `任务完成：刷新成功 ${success}/${total}`,
    refreshLogsTriggerQueued: "刷新任务已开始，正在后台执行",
    refreshLogsFailureNone: "失败账号：无",
    refreshLogsExecutionTime: "执行时间",
    refreshLogsDuration: "耗时",
    refreshLogsManual: "手动触发",
    refreshLogsScheduled: "定时任务",
    refreshLogsPageSummary: (page, totalPages, total) => `第 ${page}/${totalPages} 页，共 ${total} 条`,
    refreshLogsSuccessSummary: (success, total) => `刷新成功 ${success}/${total}`,
    refreshLogsLoading: "正在加载日志...",
    refreshLogsRunHint: "点击后会立即遍历全部邮箱并执行一次收件箱刷新"
});

text.refreshLogsPreview = "Preview HTML";
text.apiTitle = "API管理";
text.apiPlaceholder = "输入 Key 名称，例如：监控脚本、自动化工具";
text.apiCreate = "创建 API Key";
text.apiCreating = "创建中...";
text.apiCreateSuccess = "API Key 创建成功！新 Key 仅显示一次，请立即复制保存";
text.apiEmpty = "暂无 API Key，点击上方按钮创建一个";
text.apiDelete = "删除";
text.apiDeleteConfirm = "确定要删除此 API Key 吗？删除后使用该 Key 的应用将无法接入";
text.apiLastUsed = "最后使用";
text.apiNeverUsed = "从未使用";
text.apiCopied = "已复制";
text.proxyTitle = "代理池";
text.proxyDelete = "删除";
text.proxyChecking = "检测中...";
text.proxyCheckAll = "检测全部";

// 支持的代理导入格式提示
text.proxyImportFormats = [
    { pattern: "ip:port", desc: "基础格式（默认 HTTP）" },
    { pattern: "http://ip:port", desc: "HTTP 代理" },
    { pattern: "socks5://ip:port", desc: "SOCKS5 代理" },
    { pattern: "user:pass@ip:port", desc: "带认证的代理" },
    { pattern: "socks5://user:pass@ip:port", desc: "SOCKS5 带认证" }
];

async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {})
        }
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || text.requestFailed);
    }
    return data;
}

function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value ?? "";
    return div.innerHTML;
}

function decodeHtmlEntities(value) {
    const textarea = document.createElement("textarea");
    textarea.innerHTML = value;
    return textarea.value;
}

function looksLikeEscapedHtml(value) {
    const sample = value.trim().toLowerCase();
    return [
        "&lt;!doctype",
        "&lt;html",
        "&lt;body",
        "&lt;table",
        "&lt;div",
        "&lt;section",
        "&lt;p"
    ].some((marker) => sample.includes(marker));
}

function extractPreContent(value) {
    const match = value.match(/^<pre[^>]*>([\s\S]*)<\/pre>$/i);
    return match ? match[1] : "";
}

function getSelectedAccount() {
    return state.accounts.find((account) => account.id === state.selectedAccountId) || null;
}

function isMobileLayout() {
    return window.innerWidth <= MOBILE_LAYOUT_BREAKPOINT;
}

function getMailId(value) {
    if (value === null || value === undefined) {
        return null;
    }

    return String(value);
}

function padNumber(value) {
    return String(value).padStart(2, "0");
}

function formatDateTime(timestamp) {
    if (!timestamp) {
        return "-";
    }

    const date = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(date.getTime())) {
        return "-";
    }

    return `${date.getFullYear()}-${padNumber(date.getMonth() + 1)}-${padNumber(date.getDate())} ${padNumber(date.getHours())}:${padNumber(date.getMinutes())}:${padNumber(date.getSeconds())}`;
}

function formatDuration(seconds) {
    const totalSeconds = Number(seconds) || 0;
    if (totalSeconds < 60) {
        return `${totalSeconds}s`;
    }

    const minutes = Math.floor(totalSeconds / 60);
    const remainSeconds = totalSeconds % 60;
    if (minutes < 60) {
        return remainSeconds ? `${minutes}m ${remainSeconds}s` : `${minutes}m`;
    }

    const hours = Math.floor(minutes / 60);
    const remainMinutes = minutes % 60;
    if (!remainMinutes && !remainSeconds) {
        return `${hours}h`;
    }

    return `${hours}h ${remainMinutes}m ${remainSeconds}s`;
}

function parseTags(value) {
    return (value || "")
        .split(/[，,]/)
        .map((tag) => tag.trim())
        .filter(Boolean);
}

function normalizeTags(value) {
    return parseTags(value).join(", ");
}

function renderTagMarkup(value, emptyText = text.noTags, className = "tag") {
    const tags = parseTags(value);
    if (!tags.length) {
        return `<span class="muted">${escapeHtml(emptyText)}</span>`;
    }

    return tags.map((tag) => `<span class="${className}">${escapeHtml(tag)}</span>`).join("");
}

function renderRemarkMarkup(value, emptyText = EMPTY_REMARK_TEXT, className = "remark-content") {
    const remark = (value || "").trim();
    if (!remark) {
        return `<span class="muted">${escapeHtml(emptyText)}</span>`;
    }

    return `<div class="${className}">${escapeHtml(remark).replace(/\n/g, "<br>")}</div>`;
}

function getAllAvailableTags() {
    const uniqueTags = new Set();
    state.accounts.forEach((account) => {
        parseTags(account.tags).forEach((tag) => uniqueTags.add(tag));
    });

    return Array.from(uniqueTags).sort((left, right) => left.localeCompare(right, "zh-Hans-CN"));
}

function updateTagFilterOptions() {
    const availableTags = getAllAvailableTags();
    [elements.tagFilter, elements.mailTagFilter].forEach((select) => {
        const currentValue = select.value;
        const options = [
            { value: "", label: "全部标签" },
            { value: NO_TAG_FILTER_VALUE, label: "无标签" },
            ...availableTags.map((tag) => ({ value: tag, label: tag }))
        ];

        select.replaceChildren(...options.map(({ value, label }) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            return option;
        }));
        const hasCurrentValue = currentValue === ""
            || currentValue === NO_TAG_FILTER_VALUE
            || availableTags.includes(currentValue);
        select.value = hasCurrentValue ? currentValue : "";
    });
}

function getMailBodyRenderMode(body) {
    const content = (body || "").trim();
    if (!content) {
        return {
            type: "inline",
            content: `<p>${text.noContent}</p>`
        };
    }

    if (content.startsWith("<pre")) {
        const preContent = extractPreContent(content);
        if (preContent && looksLikeEscapedHtml(preContent)) {
            return {
                type: "iframe",
                content: decodeHtmlEntities(preContent)
            };
        }

        return {
            type: "inline",
            content
        };
    }

    return {
        type: "iframe",
        content: looksLikeEscapedHtml(content) ? decodeHtmlEntities(content) : content
    };
}

function setMessage(target, message, isError = false) {
    target.textContent = message;
    target.classList.toggle("is-error", isError);
}

function matchesTagFilter(account, selectedTag) {
    if (!selectedTag) {
        return true;
    }

    const tags = parseTags(account.tags);
    if (selectedTag === NO_TAG_FILTER_VALUE) {
        return tags.length === 0;
    }

    return tags.includes(selectedTag);
}

function getFilteredAccounts(keyword, selectedTag = "") {
    const query = keyword.trim().toLowerCase();
    return state.accounts.filter((account) => {
        const matchesKeyword = !query || account.email.toLowerCase().includes(query);
        return matchesKeyword && matchesTagFilter(account, selectedTag);
    });
}

function updateAutoRefreshUi() {
    elements.toggleAutoRefreshBtn.textContent = state.autoRefreshEnabled
        ? text.disableAutoRefresh
        : text.enableAutoRefresh;
    elements.autoRefreshState.textContent = state.autoRefreshEnabled
        ? text.autoRefreshOn
        : text.autoRefreshOff;
}

function updateViewActions() {
    elements.toggleAutoRefreshBtn.hidden = state.currentView !== "mails";
    elements.exportAccountsBtn.hidden = state.currentView !== "accounts";
    elements.refreshAccountsBtn.hidden = state.currentView !== "accounts";
}

function getDownloadFileNameFromDisposition(disposition) {
    if (!disposition) {
        return "emailToken.txt";
    }

    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match) {
        try {
            return decodeURIComponent(utf8Match[1]);
        } catch (error) {
            return utf8Match[1];
        }
    }

    const plainMatch = disposition.match(/filename="?([^"]+)"?/i);
    return plainMatch ? plainMatch[1] : "emailToken.txt";
}

function normalizeMailMobileStep(step) {
    if (step === "detail") {
        if (!state.selectedAccountId) {
            return "accounts";
        }
        if (!state.selectedMailId && !state.mails.length) {
            return "list";
        }
    }

    if (step === "list" && !state.selectedAccountId) {
        return "accounts";
    }

    return step;
}

function updateMailMobileView() {
    const isMobile = isMobileLayout();
    state.mobileMailStep = normalizeMailMobileStep(state.mobileMailStep);

    if (elements.mobileMailSteps) {
        elements.mobileMailSteps.hidden = !isMobile;
    }

    elements.mailStepPanels.forEach((panel) => {
        panel.classList.toggle("is-mobile-active", !isMobile || panel.dataset.mailStep === state.mobileMailStep);
    });

    elements.mailStepButtons.forEach((button) => {
        const targetStep = button.dataset.mailStepTarget;
        const needsAccount = targetStep !== "accounts";
        const needsMail = targetStep === "detail";
        const disabled = (needsAccount && !state.selectedAccountId) || (needsMail && !state.selectedMailId && !state.mails.length);

        button.disabled = disabled;
        button.classList.toggle("is-active", targetStep === state.mobileMailStep);
    });
}

function setMailMobileStep(step) {
    state.mobileMailStep = normalizeMailMobileStep(step);
    updateMailMobileView();
}

function syncMailEmptyState(listText, detailText = text.emptyMailDetail) {
    state.mailListEmptyText = listText;
    state.mailDetailEmptyText = detailText;
}

function resetMailView({
    listText = text.chooseMailList,
    detailText = text.emptyMailDetail,
    invalidateRequest = false
} = {}) {
    if (invalidateRequest) {
        state.mailLoadRequestId += 1;
    }

    state.mails = [];
    state.selectedMailId = null;
    state.loadedMailAccountId = null;
    state.loadedMailFolder = null;
    syncMailEmptyState(listText, detailText);
    renderMails();
    updateMailMobileView();
}

function updateMailActions() {
    const hasAccount = Boolean(getSelectedAccount());
    elements.copyMailAddressBtn.disabled = !hasAccount;
    updateForceRefreshBtnState();
    updateMailMobileView();
}

function updateForceRefreshBtnState() {
    if (!elements.forceRefreshMailsBtn) return;
    const hasAccount = Boolean(getSelectedAccount());
    const isRefreshing = state.mailRefreshing === true;
    elements.forceRefreshMailsBtn.disabled = !hasAccount || isRefreshing;
    elements.forceRefreshMailsBtn.textContent = isRefreshing ? "刷新中..." : "立即刷新";
}

async function forceRefreshMails() {
    if (!state.selectedAccountId) {
        setMessage(elements.mailMessage, text.chooseAccountFirst, true);
        return;
    }
    const accountId = state.selectedAccountId;
    const folder = state.selectedFolder;
    const requestId = ++state.mailLoadRequestId;

    state.mailRefreshing = true;
    updateForceRefreshBtnState();
    setMessage(elements.mailMessage, "正在强制拉取最新邮件...", false);
    syncMailEmptyState(text.loadingMails, text.loadingMails);

    try {
        const data = await api(
            `/api/accounts/${accountId}/mails/refresh?folder=${folder}`,
            { method: "POST" }
        );
        if (requestId !== state.mailLoadRequestId || state.selectedAccountId !== accountId || state.selectedFolder !== folder) {
            return;
        }
        // 强制更新 UI，无论内容是否变化
        updateMailsFromData(data, accountId, folder);
        if (data.refresh_error) {
            setMessage(elements.mailMessage, `拉取异常：${data.refresh_error}`, true);
        } else {
            setMessage(elements.mailMessage, `已拉取最新邮件（${state.mails.length} 封）`, false);
        }
        updateMailMobileView();
    } catch (error) {
        if (requestId !== state.mailLoadRequestId) return;
        setMessage(elements.mailMessage, error.message, true);
    } finally {
        state.mailRefreshing = false;
        updateForceRefreshBtnState();
    }
}

function renderAccountButtons(container, accounts, options) {
    const {
        onSelect,
        onSetTags,
        onSetRemark,
        showSetTagsButton = false,
        showSetRemarkButton = false
    } = options;
    if (!accounts.length) {
        container.innerHTML = `<div class="empty-state">${text.noMatchingAccounts}</div>`;
        return;
    }

    container.innerHTML = accounts.map((account) => {
        const tags = renderTagMarkup(account.tags);
        const activeClass = account.id === state.selectedAccountId ? " account-item-active" : "";
        const checked = state.selectedAccountIds.has(account.id) ? " checked" : "";
        const checkbox = `
            <label class="account-checkbox" data-id="${account.id}">
                <input type="checkbox" class="account-select-checkbox" data-id="${account.id}"${checked}>
                <span class="account-checkbox-box" aria-hidden="true">
                    <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M4.5 10.5L8 14L15.5 6.5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </span>
            </label>
        `;
        const accountButton = `
            <button class="account-item account-select-btn${activeClass}" type="button" data-id="${account.id}">
                <strong class="account-item-email">${escapeHtml(account.email)}</strong>
            </button>
        `;

        if (!showSetTagsButton && !showSetRemarkButton) {
            return `
                <div class="account-item-shell${activeClass}">
                    <div class="account-item-top">
                        ${checkbox}
                        ${accountButton}
                    </div>
                    <div class="tag-row">${tags}</div>
                </div>
            `;
        }

        const actions = [];
        if (showSetTagsButton) {
            actions.push(`<button class="account-tag-button" type="button" data-action="tag" data-id="${account.id}">设置标签</button>`);
        }
        if (showSetRemarkButton) {
            actions.push(`<button class="account-tag-button" type="button" data-action="remark" data-id="${account.id}">设置备注</button>`);
        }

        return `
            <div class="account-item-shell${activeClass}">
                <div class="account-item-top">
                    ${checkbox}
                    ${accountButton}
                </div>
                <div class="account-item-meta">
                    <div class="tag-row">${tags}</div>
                    <div class="account-meta-actions">${actions.join("")}</div>
                </div>
            </div>
        `;
    }).join("");

    container.querySelectorAll(".account-select-btn").forEach((button) => {
        button.addEventListener("click", () => onSelect(Number(button.dataset.id)));
    });

    // 复选框点击:阻止冒泡(避免触发外层按钮),切换选中状态
    container.querySelectorAll(".account-select-checkbox").forEach((checkbox) => {
        checkbox.addEventListener("click", (event) => {
            event.stopPropagation();
        });
        checkbox.addEventListener("change", () => {
            toggleAccountSelection(Number(checkbox.dataset.id));
        });
    });

    // 整个 checkbox label 也要阻止冒泡,避免点击 label 触发外层 button
    container.querySelectorAll(".account-checkbox").forEach((label) => {
        label.addEventListener("click", (event) => {
            event.stopPropagation();
        });
    });

    if (showSetTagsButton && onSetTags) {
        container.querySelectorAll('[data-action="tag"]').forEach((button) => {
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                onSetTags(Number(button.dataset.id));
            });
        });
    }
    if (showSetRemarkButton && onSetRemark) {
        container.querySelectorAll('[data-action="remark"]').forEach((button) => {
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                onSetRemark(Number(button.dataset.id));
            });
        });
    }
}

function renderAccounts() {
    const accountMatches = getFilteredAccounts(elements.searchInput.value, elements.tagFilter.value);
    const mailMatches = getFilteredAccounts(elements.mailSearchInput.value, elements.mailTagFilter.value);

    elements.accountCount.textContent = text.accountCount(state.accounts.length);
    elements.accountFilterSummary.textContent = text.visibleAccountCount(accountMatches.length, state.accounts.length);
    elements.mailFilterSummary.textContent = text.visibleAccountCount(mailMatches.length, state.accounts.length);
    renderAccountButtons(elements.accountList, accountMatches, {
        onSelect: selectAccountForManage
    });
    renderAccountButtons(elements.mailAccountList, mailMatches, {
        onSelect: selectAccountForMail,
        onSetTags: promptAndSaveTags,
        onSetRemark: promptAndSaveRemark,
        showSetTagsButton: true,
        showSetRemarkButton: true
    });

    // 同步两个列表的批量操作栏(全选状态、已选数量、删除按钮可用性)
    updateBulkUI("accounts", accountMatches);
    updateBulkUI("mails", mailMatches);
}

// ─── 批量选择 / 删除 ───────────────────────────────
function getVisibleAccountsForTarget(target) {
    if (target === "mails") {
        return getFilteredAccounts(elements.mailSearchInput.value, elements.mailTagFilter.value);
    }
    return getFilteredAccounts(elements.searchInput.value, elements.tagFilter.value);
}

function toggleAccountSelection(accountId) {
    if (state.selectedAccountIds.has(accountId)) {
        state.selectedAccountIds.delete(accountId);
    } else {
        state.selectedAccountIds.add(accountId);
    }
    // 选中状态变化后,两个列表的批量操作栏都要刷新
    updateBulkUI("accounts", getVisibleAccountsForTarget("accounts"));
    updateBulkUI("mails", getVisibleAccountsForTarget("mails"));
}

function toggleSelectAll(target) {
    const visibleAccounts = getVisibleAccountsForTarget(target);
    const visibleIds = visibleAccounts.map((account) => account.id);
    const allSelected = visibleIds.length > 0 && visibleIds.every((id) => state.selectedAccountIds.has(id));

    if (allSelected) {
        // 取消当前可见项的选中(保留其它筛选条件下的选中)
        visibleIds.forEach((id) => state.selectedAccountIds.delete(id));
    } else {
        // 全选当前可见项
        visibleIds.forEach((id) => state.selectedAccountIds.add(id));
    }

    // 重新渲染复选框勾选状态(只更新 checked 属性,避免重渲染整个列表)
    document.querySelectorAll(".account-select-checkbox").forEach((checkbox) => {
        const id = Number(checkbox.dataset.id);
        checkbox.checked = state.selectedAccountIds.has(id);
    });

    updateBulkUI("accounts", getVisibleAccountsForTarget("accounts"));
    updateBulkUI("mails", getVisibleAccountsForTarget("mails"));
}

function updateBulkUI(target, visibleAccounts) {
    const selectAllEl = target === "mails" ? elements.mailsBulkSelectAll : elements.accountsBulkSelectAll;
    const deleteBtnEl = target === "mails" ? elements.mailsBulkDeleteBtn : elements.accountsBulkDeleteBtn;
    const refreshBtnEl = target === "mails" ? elements.mailsBulkRefreshBtn : elements.accountsBulkRefreshBtn;
    const row = document.querySelector(`.bulk-select-row[data-bulk-target="${target}"]`);
    if (!row) return;

    const visibleIds = (visibleAccounts || []).map((account) => account.id);
    const visibleSelectedCount = visibleIds.filter((id) => state.selectedAccountIds.has(id)).length;
    const totalSelectedCount = state.selectedAccountIds.size;

    // 全选 checkbox: 三态显示(全选 / 部分选中 / 未选)
    if (selectAllEl) {
        if (visibleIds.length === 0) {
            selectAllEl.checked = false;
            selectAllEl.indeterminate = false;
        } else if (visibleSelectedCount === visibleIds.length) {
            selectAllEl.checked = true;
            selectAllEl.indeterminate = false;
        } else if (visibleSelectedCount === 0) {
            selectAllEl.checked = false;
            selectAllEl.indeterminate = false;
        } else {
            selectAllEl.checked = false;
            selectAllEl.indeterminate = true;
        }
    }

    // 已选数量(基于所有选中,而非仅当前可见的选中,让用户清楚全局选中数量)
    const selectedCountEl = row.querySelector(".bulk-selected-count");
    const totalCountEl = row.querySelector(".bulk-total-count");
    if (selectedCountEl) selectedCountEl.textContent = String(totalSelectedCount);
    if (totalCountEl) totalCountEl.textContent = String(visibleIds.length);

    // 删除按钮 / 刷新按钮:有选中即可用(刷新中除外)
    const isBulkRefreshing = state.bulkRefreshing === true;
    const canAct = totalSelectedCount > 0 && !isBulkRefreshing;
    if (deleteBtnEl) {
        deleteBtnEl.disabled = !canAct;
    }
    if (refreshBtnEl) {
        refreshBtnEl.disabled = !canAct;
        if (!isBulkRefreshing) {
            refreshBtnEl.textContent = "刷新选中";
        }
    }
}

function clearAccountSelection() {
    state.selectedAccountIds.clear();
    document.querySelectorAll(".account-select-checkbox").forEach((checkbox) => {
        checkbox.checked = false;
    });
    updateBulkUI("accounts", getVisibleAccountsForTarget("accounts"));
    updateBulkUI("mails", getVisibleAccountsForTarget("mails"));
}

function openDeleteConfirmModal(ids) {
    if (!ids || ids.length === 0) {
        setMessage(elements.accountMessage, text.bulkDeleteNone, true);
        return;
    }

    const accounts = ids
        .map((id) => state.accounts.find((account) => account.id === id))
        .filter(Boolean);

    elements.deleteConfirmCount.textContent = `${ids.length} 个`;
    // 仅展示前 20 个邮箱,避免列表过长
    const previewAccounts = accounts.slice(0, 20);
    const moreCount = accounts.length - previewAccounts.length;
    elements.deleteConfirmList.innerHTML = previewAccounts
        .map((account) => `<div class="delete-confirm-item">${escapeHtml(account.email)}</div>`)
        .join("") + (moreCount > 0 ? `<div class="delete-confirm-more muted">…还有 ${moreCount} 个账号</div>` : "");

    elements.deleteConfirmSaveBtn.dataset.ids = JSON.stringify(ids);
    setMessage(elements.deleteConfirmMessage, "", false);
    openModal(elements.deleteConfirmModal);
}

function closeDeleteConfirmModal() {
    closeModal(elements.deleteConfirmModal);
    delete elements.deleteConfirmSaveBtn.dataset.ids;
    setMessage(elements.deleteConfirmMessage, "", false);
}

async function confirmDeleteAccounts() {
    const idsRaw = elements.deleteConfirmSaveBtn.dataset.ids;
    if (!idsRaw) return;

    let ids;
    try {
        ids = JSON.parse(idsRaw);
    } catch {
        ids = [];
    }
    if (!ids.length) {
        closeDeleteConfirmModal();
        return;
    }

    elements.deleteConfirmSaveBtn.disabled = true;
    elements.deleteConfirmSaveBtn.textContent = text.deletingAccounts;
    setMessage(elements.deleteConfirmMessage, text.deletingAccounts, false);

    try {
        let result;
        if (ids.length === 1) {
            // 单条直接走 DELETE
            await api(`/api/accounts/${ids[0]}`, { method: "DELETE" });
            result = { deleted: 1, missing: [] };
        } else {
            result = await api("/api/accounts/batch-delete", {
                method: "POST",
                body: JSON.stringify({ ids })
            });
        }

        // 清理选中状态
        ids.forEach((id) => state.selectedAccountIds.delete(id));
        // 如果当前选中账号被删除,清理 selectedAccountId
        if (state.selectedAccountId && ids.includes(state.selectedAccountId)) {
            state.selectedAccountId = null;
            updateSelectedAccountSummary();
            resetMailView({ invalidateRequest: true });
        }

        closeDeleteConfirmModal();

        const message = result.missing && result.missing.length > 0
            ? text.bulkDeletePartial(result.deleted, result.missing.length)
            : text.bulkDeleteSuccess(result.deleted);
        setMessage(elements.accountMessage, message, false);

        await loadAccounts();
        renderAccounts();
    } catch (error) {
        setMessage(elements.deleteConfirmMessage, error.message, true);
    } finally {
        elements.deleteConfirmSaveBtn.disabled = false;
        elements.deleteConfirmSaveBtn.textContent = "确认删除";
    }
}

async function deleteSingleAccount(accountId) {
    if (!accountId) return;
    if (!window.confirm(text.deleteSingleConfirm)) {
        return;
    }

    elements.deleteAccountBtn.disabled = true;
    elements.deleteAccountBtn.textContent = text.deletingAccounts;

    try {
        await api(`/api/accounts/${accountId}`, { method: "DELETE" });
        state.selectedAccountIds.delete(accountId);
        state.selectedAccountId = null;
        updateSelectedAccountSummary();
        resetMailView({ invalidateRequest: true });
        setMessage(elements.accountMessage, text.deleteSingleSuccess, false);
        await loadAccounts();
        renderAccounts();
    } catch (error) {
        setMessage(elements.accountMessage, error.message, true);
    } finally {
        elements.deleteAccountBtn.disabled = !state.selectedAccountId;
        elements.deleteAccountBtn.textContent = "删除此账号";
    }
}

// ─── 批量刷新邮件 ─────────────────────────────────────
async function refreshSelectedAccounts() {
    const ids = Array.from(state.selectedAccountIds);
    if (ids.length === 0) {
        setMessage(elements.accountMessage, text.bulkRefreshNone, true);
        return;
    }

    if (state.bulkRefreshing) return;
    state.bulkRefreshing = true;

    // 优先刷新邮件视图当前所在的文件夹,否则用收件箱
    const folder = state.selectedFolder || "inbox";

    // 同时禁用两个面板的刷新按钮,避免重复点击
    const refreshBtns = [elements.accountsBulkRefreshBtn, elements.mailsBulkRefreshBtn];
    refreshBtns.forEach((btn) => {
        if (!btn) return;
        btn.disabled = true;
        btn.textContent = text.bulkRefreshing;
    });
    setMessage(elements.accountMessage, text.bulkRefreshing, false);

    try {
        const result = await api("/api/accounts/batch-refresh", {
            method: "POST",
            body: JSON.stringify({ ids, folder })
        });

        const triggered = result.triggered || 0;
        const skipped = result.skipped || 0;
        const missing = (result.missing || []).length;

        let message = text.bulkRefreshTriggered(triggered, skipped, folder);
        if (missing > 0) {
            message += `，其中 ${missing} 个账号不存在或已删除`;
        }
        setMessage(elements.accountMessage, message, false);

        // 如果当前邮件视图有选中的账号,触发一次重新加载拿最新缓存
        if (state.selectedAccountId && ids.includes(state.selectedAccountId)) {
            // 不阻塞,延迟 800ms 等后台刷新写出一点进度
            setTimeout(() => {
                if (state.selectedAccountId) loadMails({ silent: true });
            }, 800);
        }
    } catch (error) {
        setMessage(elements.accountMessage, error.message, true);
    } finally {
        state.bulkRefreshing = false;
        // 同步两个面板的按钮状态
        updateBulkUI("accounts", getVisibleAccountsForTarget("accounts"));
        updateBulkUI("mails", getVisibleAccountsForTarget("mails"));
    }
}

function openModal(modalEl) {
    if (!modalEl) return;
    modalEl.hidden = false;
    // 强制重排,触发 transition
    void modalEl.offsetWidth;
    modalEl.classList.add("is-visible");
    document.body.classList.add("modal-open");
}

function closeModal(modalEl) {
    if (!modalEl) return;
    modalEl.classList.remove("is-visible");
    document.body.classList.remove("modal-open");
    // 等过渡结束后再隐藏,避免突兀消失
    setTimeout(() => {
        if (modalEl) modalEl.hidden = true;
    }, 180);
}

function renderRefreshLogPagination() {
    const totalPages = Math.max(state.logsPagination.totalPages || 1, 1);
    const currentPage = Math.min(state.logsPagination.page || 1, totalPages);

    elements.refreshLogPage.replaceChildren(...Array.from({ length: totalPages }, (_, index) => {
        const option = document.createElement("option");
        const page = index + 1;
        option.value = String(page);
        option.textContent = `第 ${page} 页`;
        return option;
    }));
    elements.refreshLogPage.value = String(currentPage);

    elements.refreshLogPageSize.replaceChildren(...LOG_PAGE_SIZE_OPTIONS.map((pageSize) => {
        const option = document.createElement("option");
        option.value = String(pageSize);
        option.textContent = `${pageSize} 条`;
        return option;
    }));
    elements.refreshLogPageSize.value = String(state.logsPagination.pageSize);

    // 重建自定义下拉（动态填充的 option 需要重新渲染）
    [_cselectInstances.get(elements.refreshLogPage), _cselectInstances.get(elements.refreshLogPageSize)]
        .filter(Boolean).forEach((inst) => inst.rebuildOptions());

    elements.refreshLogPage.disabled = state.logsPagination.total === 0;
    elements.refreshLogPaginationSummary.textContent = text.refreshLogsPageSummary(
        currentPage,
        totalPages,
        state.logsPagination.total
    );
}

function renderRefreshLogs() {
    renderRefreshLogPagination();

    if (!state.tokenRefreshLogs.length) {
        elements.refreshLogList.innerHTML = `<div class="empty-state">${text.refreshLogsEmpty}</div>`;
        return;
    }

    elements.refreshLogList.innerHTML = state.tokenRefreshLogs.map((log) => {
        const failureItems = Array.isArray(log.failure_items) ? log.failure_items : [];
        const failureMarkup = failureItems.length
            ? failureItems.map((item) => `
                <div class="refresh-log-failure">
                    <strong>${escapeHtml(item.email || "-")}</strong>
                    <span>${escapeHtml(item.error || "-")}</span>
                </div>
            `).join("")
            : `<div class="refresh-log-failure is-empty">${escapeHtml(text.refreshLogsFailureNone)}</div>`;
        const triggerLabel = log.trigger_type === "scheduled"
            ? text.refreshLogsScheduled
            : text.refreshLogsManual;
        const statusClass = Number(log.failed_count) > 0 ? " is-partial" : " is-success";
        const previewButtonMarkup = log.has_html
            ? `<button class="button button-ghost refresh-log-preview-btn" type="button" data-preview-log-id="${Number(log.id)}">${escapeHtml(text.refreshLogsPreview)}</button>`
            : "";

        return `
            <article class="refresh-log-card${statusClass}">
                <div class="refresh-log-card-head">
                    <span class="pill">${escapeHtml(triggerLabel)}</span>
                    <span class="refresh-log-duration">${escapeHtml(text.refreshLogsDuration)}：${escapeHtml(formatDuration(log.duration_seconds))}</span>
                </div>
                <h3>${escapeHtml(text.refreshLogsSuccessSummary(log.success_count, log.total_count))}</h3>
                <div class="refresh-log-failures">${failureMarkup}</div>
                <div class="refresh-log-time">
                    <span>${escapeHtml(text.refreshLogsExecutionTime)}：</span>
                    <time>${escapeHtml(formatDateTime(log.finished_at || log.started_at || log.created_at))}</time>
                </div>
            </article>
        `;
    }).join("");
}

function renderRefreshLogs() {
    renderRefreshLogPagination();

    if (!state.tokenRefreshLogs.length) {
        elements.refreshLogList.innerHTML = `<div class="empty-state">${text.refreshLogsEmpty}</div>`;
        return;
    }

    elements.refreshLogList.innerHTML = state.tokenRefreshLogs.map((log) => {
        const failureItems = Array.isArray(log.failure_items) ? log.failure_items : [];
        const failureMarkup = failureItems.length
            ? failureItems.map((item) => `
                <div class="refresh-log-failure">
                    <strong>${escapeHtml(item.email || "-")}</strong>
                    <span>${escapeHtml(item.error || "-")}</span>
                </div>
            `).join("")
            : `<div class="refresh-log-failure is-empty">${escapeHtml(text.refreshLogsFailureNone)}</div>`;
        const triggerLabel = log.trigger_type === "scheduled"
            ? text.refreshLogsScheduled
            : text.refreshLogsManual;
        const statusClass = Number(log.failed_count) > 0 ? " is-partial" : " is-success";
        const previewButtonMarkup = log.has_html
            ? `<button class="button button-ghost refresh-log-preview-btn" type="button" data-preview-log-id="${Number(log.id)}">${escapeHtml(text.refreshLogsPreview)}</button>`
            : "";

        return `
            <article class="refresh-log-card${statusClass}">
                <div class="refresh-log-card-head">
                    <span class="pill">${escapeHtml(triggerLabel)}</span>
                    <span class="refresh-log-duration">${escapeHtml(text.refreshLogsDuration)}: ${escapeHtml(formatDuration(log.duration_seconds))}</span>
                </div>
                <h3>${escapeHtml(text.refreshLogsSuccessSummary(log.success_count, log.total_count))}</h3>
                <div class="refresh-log-failures">${failureMarkup}</div>
                <div class="refresh-log-time">
                    <span>${escapeHtml(text.refreshLogsExecutionTime)}:</span>
                    <time>${escapeHtml(formatDateTime(log.finished_at || log.started_at || log.created_at))}</time>
                </div>
                <div class="refresh-log-actions">${previewButtonMarkup}</div>
            </article>
        `;
    }).join("");
}

function openRefreshLogPreview(logId) {
    if (!logId) {
        return;
    }

    const url = `/token-refresh-logs/${encodeURIComponent(String(logId))}/preview`;
    window.open(url, "_blank", "noopener");
}

function renderMailDetail() {
    const currentMail = state.mails.find((mail) => getMailId(mail.id) === getMailId(state.selectedMailId));
    if (!currentMail) {
        elements.mailDetail.className = "mail-detail empty-state";
        elements.mailDetail.textContent = text.emptyMailDetail;
        updateMailMobileView();
        return;
    }

    const bodyRender = getMailBodyRenderMode(currentMail.body);
    elements.mailDetail.className = "mail-detail";
    elements.mailDetail.innerHTML = `
        <header class="mail-detail-head">
            <h3>${escapeHtml(currentMail.subject || text.noSubject)}</h3>
            <p>From: ${escapeHtml(currentMail.mail_from || "-")}</p>
            <p>To: ${escapeHtml(currentMail.mail_to || "-")}</p>
            <p>Time: ${escapeHtml(currentMail.mail_dt || "-")}</p>
        </header>
        <section class="mail-detail-body"></section>
    `;

    const bodyContainer = elements.mailDetail.querySelector(".mail-detail-body");
    if (bodyRender.type === "iframe") {
        const iframe = document.createElement("iframe");
        iframe.setAttribute("sandbox", "allow-popups allow-popups-to-escape-sandbox");
        iframe.setAttribute("referrerpolicy", "no-referrer");
        iframe.srcdoc = bodyRender.content;
        bodyContainer.replaceChildren(iframe);
        updateMailMobileView();
        return;
    }

    bodyContainer.innerHTML = bodyRender.content;
    updateMailMobileView();
}

function renderMails() {
    if (!state.mails.length) {
        elements.mailList.className = "mail-list empty-state";
        elements.mailList.textContent = state.mailListEmptyText;
        elements.mailDetail.className = "mail-detail empty-state";
        elements.mailDetail.textContent = state.mailDetailEmptyText;
        return;
    }

    if (!state.selectedMailId && state.mails[0]) {
        state.selectedMailId = getMailId(state.mails[0].id);
    }

    elements.mailList.className = "mail-list";
    elements.mailList.innerHTML = state.mails.map((mail) => {
        const activeClass = getMailId(mail.id) === getMailId(state.selectedMailId) ? " mail-item-active" : "";
        return `
            <button class="mail-item${activeClass}" type="button" data-id="${escapeHtml(getMailId(mail.id) || "")}">
                <strong>${escapeHtml(mail.subject || text.noSubject)}</strong>
                <span>${escapeHtml(mail.mail_from || "-")}</span>
                <time>${escapeHtml(mail.mail_dt || "")}</time>
            </button>
        `;
    }).join("");

    elements.mailList.querySelectorAll(".mail-item").forEach((button) => {
        button.addEventListener("click", () => {
            state.selectedMailId = getMailId(button.dataset.id);
            renderMails();
            setMailMobileStep("detail");
        });
    });

    renderMailDetail();
}

function updateSelectedAccountSummary() {
    const account = getSelectedAccount();
    elements.selectedEmail.textContent = account ? account.email : "请选择邮箱账号";
    elements.tagsInput.value = account ? account.tags : "";
    elements.mailPanelTitle.textContent = account ? `${account.email} 的邮件` : "请选择邮箱";
    updateMailActions();
    renderAccounts();
}

const baseUpdateSelectedAccountSummary = updateSelectedAccountSummary;
updateSelectedAccountSummary = function updateSelectedAccountSummaryWithRemark() {
    baseUpdateSelectedAccountSummary();
    const account = getSelectedAccount();
    elements.remarkInput.value = account ? (account.remark || "") : "";
    elements.selectedRemark.innerHTML = renderRemarkMarkup(account ? account.remark : "");
    elements.mailAccountRemark.innerHTML = renderRemarkMarkup(account ? account.remark : "");
    elements.mailPanelTitle.textContent = account ? `${account.email} 的邮件` : "请选择邮箱";
    // 单条删除按钮:仅在选中账号时可用
    if (elements.deleteAccountBtn) {
        elements.deleteAccountBtn.disabled = !account;
    }
};

function switchView(view) {
    state.currentView = view;
    elements.menuItems.forEach((item) => {
        item.classList.toggle("is-active", item.dataset.view === view);
    });
    elements.viewPanels.forEach((panel) => {
        panel.classList.toggle("is-visible", panel.dataset.panel === view);
    });
    elements.viewTitle.textContent = ({
        accounts: text.accountsTitle,
        mails: text.mailsTitle,
        logs: text.logsTitle,
        api: text.apiTitle,
        proxy: text.proxyTitle,
        settings: text.settingsTitle
    })[view] || text.accountsTitle;
    updateViewActions();
    updateMailMobileView();
    // 切换到设置页时加载版本信息
    if (view === "settings") {
        loadVersionInfo();
    }
}

function stopAutoRefresh() {
    if (state.accountRefreshTimer) {
        window.clearInterval(state.accountRefreshTimer);
        state.accountRefreshTimer = null;
    }
    if (state.mailRefreshTimer) {
        window.clearInterval(state.mailRefreshTimer);
        state.mailRefreshTimer = null;
    }
}

// 只管理邮件刷新定时器，不影响账号刷新定时器
// 这样账号定时器不会被 loadAccounts 间接重置，保证稳定运行
function refreshMailTimerState() {
    const shouldRun = state.autoRefreshEnabled
        && state.selectedAccountId
        && state.currentView === "mails";

    if (shouldRun) {
        if (!state.mailRefreshTimer) {
            state.mailRefreshTimer = window.setInterval(() => {
                loadMails({ silent: true });
            }, AUTO_REFRESH_INTERVAL);
        }
    } else if (state.mailRefreshTimer) {
        window.clearInterval(state.mailRefreshTimer);
        state.mailRefreshTimer = null;
    }
}

function startAutoRefresh() {
    stopAutoRefresh();
    updateAutoRefreshUi();

    if (!state.autoRefreshEnabled) {
        return;
    }

    state.accountRefreshTimer = window.setInterval(() => {
        loadAccounts({ silent: true });
    }, AUTO_REFRESH_INTERVAL);

    refreshMailTimerState();
}

async function loadAccounts({ silent = false } = {}) {
    try {
        const data = await api("/api/accounts");
        state.accounts = (data.items || []).slice().sort((left, right) => right.id - left.id);
        updateTagFilterOptions();

        if (state.selectedAccountId && !state.accounts.some((item) => item.id === state.selectedAccountId)) {
            state.selectedAccountId = null;
            resetMailView({ invalidateRequest: true });
        }

        // 清理已不存在的选中账号(防止选中已被删除的账号)
        if (state.selectedAccountIds.size > 0) {
            const existingIds = new Set(state.accounts.map((account) => account.id));
            for (const id of Array.from(state.selectedAccountIds)) {
                if (!existingIds.has(id)) {
                    state.selectedAccountIds.delete(id);
                }
            }
        }

        updateSelectedAccountSummary();
        renderMails();
        refreshMailTimerState();
        updateMailMobileView();
    } catch (error) {
        if (!silent) {
            setMessage(elements.accountMessage, error.message, true);
        }
    }
}

function setRefreshLogRunLoading(isLoading) {
    state.tokenRefreshTaskRunning = isLoading;
    elements.refreshLogRunBtn.disabled = isLoading;
    elements.refreshLogRunBtn.textContent = isLoading
        ? text.refreshLogsRunning
        : text.refreshLogsRun;
}

async function loadTokenRefreshLogs({ silent = false } = {}) {
    try {
        if (!silent) {
            setMessage(elements.refreshLogMessage, text.refreshLogsLoading, false);
        }

        const { page, pageSize } = state.logsPagination;
        const data = await api(`/api/token-refresh-logs?page=${page}&page_size=${pageSize}`);
        const pagination = data.pagination || {};

        state.tokenRefreshLogs = data.items || [];
        state.logsPagination.page = pagination.page || page;
        state.logsPagination.pageSize = pagination.page_size || pageSize;
        state.logsPagination.total = pagination.total || 0;
        state.logsPagination.totalPages = pagination.total_pages || 1;
        renderRefreshLogs();

        if (!silent) {
            setMessage(elements.refreshLogMessage, text.refreshLogsLoaded(state.logsPagination.total), false);
        }
    } catch (error) {
        if (!silent) {
            setMessage(elements.refreshLogMessage, error.message, true);
        }
    }
}

async function triggerTokenRefreshTask() {
    setRefreshLogRunLoading(true);
    setMessage(elements.refreshLogMessage, text.refreshLogsRunHint, false);

    try {
        await api("/api/token-refresh-logs/trigger", {
            method: "POST"
        });
        setMessage(
            elements.refreshLogMessage,
            text.refreshLogsTriggerQueued,
            false
        );
    } catch (error) {
        setMessage(elements.refreshLogMessage, error.message, true);
    } finally {
        setRefreshLogRunLoading(false);
    }
}

async function loadMails({ silent = false } = {}) {
    if (!state.selectedAccountId) {
        setMailMobileStep("accounts");
        return;
    }

    const accountId = state.selectedAccountId;
    const folder = state.selectedFolder;
    const requestId = ++state.mailLoadRequestId;

    if (!silent) {
        resetMailView({
            listText: text.loadingMails,
            detailText: text.loadingMails
        });
        setMessage(elements.mailMessage, text.loadingMails, false);
    }

    // 第一次请求：拿到缓存立即渲染（"秒出"）
    let data;
    try {
        data = await api(`/api/accounts/${accountId}/mails?folder=${folder}`);
    } catch (error) {
        if (requestId !== state.mailLoadRequestId) return;
        // 首次取件失败:常见于新导入账号 token 还未预热或瞬时网络问题。
        // 主动触发一次后台批量刷新(单个账号),并等待最多 30 秒后重试一次。
        const isLikelyFirstFailure = !error.message || /400|invalid|token|network|refresh/i.test(error.message);
        if (isLikelyFirstFailure) {
            try {
                await api("/api/accounts/batch-refresh", {
                    method: "POST",
                    body: JSON.stringify({ ids: [accountId], folder })
                });
                // 等待后台刷新完成(后端 wait_for_refresh 最多 30 秒)
                data = await api(`/api/accounts/${accountId}/mails?folder=${folder}&wait=1`);
            } catch (retryError) {
                syncMailEmptyState(text.emptyMailList, text.emptyMailDetail);
                renderMails();
                setMessage(elements.mailMessage, retryError.message || error.message, true);
                updateMailMobileView();
                return;
            }
        } else {
            syncMailEmptyState(text.emptyMailList, text.emptyMailDetail);
            renderMails();
            setMessage(elements.mailMessage, error.message, true);
            updateMailMobileView();
            return;
        }
    }

    if (requestId !== state.mailLoadRequestId || state.selectedAccountId !== accountId || state.selectedFolder !== folder) {
        return;
    }

    // 立即渲染缓存内容
    updateMailsFromData(data, accountId, folder);
    updateForceRefreshBtnState();

    // 缓存返回 + 后台正在刷新 + 缓存不是最新 → 发起 wait 请求等最新结果
    if (data.cached && data.refreshing && !data.is_fresh) {
        setMessage(elements.mailMessage, "已加载缓存，正在拉取最新邮件...", false);
        await fetchMailsWithWait({ silent: true, requestId, accountId, folder });
        return;
    }

    // 缓存已是最新 → 显示"已加载"
    if (data.cached && data.is_fresh) {
        let hint = text.loadedMails(state.mails.length);
        if (data.updated_at) {
            hint = `已加载 ${state.mails.length} 封邮件（缓存 ${formatDateTime(data.updated_at)}）`;
        }
        setMessage(elements.mailMessage, hint, false);
    } else if (!data.cached) {
        // 无缓存，实时拉取
        setMessage(elements.mailMessage, text.loadedMails(state.mails.length), false);
    } else if (data.stale) {
        setMessage(elements.mailMessage, "使用旧缓存（取件失败，可能是令牌失效或密码错误）", false);
    }
    updateMailMobileView();
}

// 把后端返回的 mails 数据应用到 state 并重新渲染
function updateMailsFromData(data, accountId, folder) {
    state.mails = data.items || [];
    state.loadedMailAccountId = accountId;
    state.loadedMailFolder = folder;
    syncMailEmptyState(text.emptyMailList, text.emptyMailDetail);
    // 检查当前选中的邮件是否还在新列表里
    if (!state.mails.some((mail) => getMailId(mail.id) === getMailId(state.selectedMailId))) {
        state.selectedMailId = null;
    }
    renderMails();
}

// 等待后台刷新完成并刷新视图
async function fetchMailsWithWait({ silent, requestId, accountId, folder }) {
    try {
        const data = await api(
            `/api/accounts/${accountId}/mails?folder=${folder}&wait=true`,
            { method: "GET" }
        );
        if (requestId !== state.mailLoadRequestId || state.selectedAccountId !== accountId || state.selectedFolder !== folder) {
            return;
        }

        // 关键：拿到新数据后，无论内容是否变化都强制更新 UI
        // （之前用 "新旧 first id 比较" 判断，导致很多情况下 UI 不更新）
        updateMailsFromData(data, accountId, folder);

        if (data.refresh_error) {
            setMessage(elements.mailMessage, `拉取异常：${data.refresh_error}`, true);
        } else {
            const mailCount = state.mails.length;
            if (data.updated_at) {
                setMessage(elements.mailMessage, `已更新到最新邮件（${mailCount} 封，${formatDateTime(data.updated_at)}）`, false);
            } else {
                setMessage(elements.mailMessage, `已更新到最新邮件（${mailCount} 封）`, false);
            }
        }
        updateMailMobileView();
    } catch (error) {
        if (!silent) {
            setMessage(elements.mailMessage, error.message, true);
        }
    }
}

function selectAccountForManage(accountId) {
    state.selectedAccountId = accountId;
    updateSelectedAccountSummary();
    setMessage(elements.accountMessage, text.selectedAccountHint, false);
}

async function selectAccountForMail(accountId) {
    state.selectedAccountId = accountId;
    state.selectedMailId = null;
    updateSelectedAccountSummary();
    setMailMobileStep("list");
    await loadMails();
    refreshMailTimerState();
}

async function importAccounts() {
    const value = elements.importInput.value.trim();
    if (!value) {
        setMessage(elements.importMessage, text.importInputRequired, true);
        return;
    }

    const protocol = elements.importProtocolSelect ? elements.importProtocolSelect.value : "graph";
    const serverValue = elements.importServerInput ? elements.importServerInput.value.trim() : "";
    const sslValue = elements.importSslSelect ? elements.importSslSelect.value : "1";

    const payload = { text: value, protocol, mail_use_ssl: parseInt(sslValue, 10) || 1 };
    if (serverValue) {
        // 支持 host:port 或纯 host
        const parts = serverValue.split(":");
        if (parts.length >= 2) {
            payload.mail_server = parts[0].trim();
            const port = parseInt(parts[1], 10);
            if (port > 0) payload.mail_port = port;
        } else {
            payload.mail_server = serverValue;
        }
    }

    try {
        const data = await api("/api/accounts/import", {
            method: "POST",
            body: JSON.stringify(payload)
        });
        // 显示导入摘要 + 字段识别详情
        let summary = text.importFinished(data);
        if (data.details && data.details.length > 0) {
            const detailLines = data.details.map(d => {
                if (d.status === "skipped") {
                    return `  ⚠ ${d.line} → 跳过：${d.reason}`;
                }
                const swap = d.swapped ? "（字段顺序已自动交换）" : "";
                return `  ✓ ${d.email} | ${d.protocol} | ${d.refresh_token_type}${swap}`;
            });
            summary += `\n\n识别详情：\n${detailLines.join("\n")}`;
        }
        setMessage(elements.importMessage, summary, false);
        elements.importInput.value = "";
        await loadAccounts();
    } catch (error) {
        setMessage(elements.importMessage, error.message, true);
    }
}

function updateImportProtocolVisibility() {
    if (!elements.importProtocolSelect) return;
    const protocol = elements.importProtocolSelect.value;
    // 只在用户明确选了 imap/pop3 时才显示服务器配置（auto/graph 不需要）
    const showServer = protocol === "imap" || protocol === "pop3";
    if (elements.importServerRow) elements.importServerRow.hidden = !showServer;
    if (elements.importSslRow) elements.importSslRow.hidden = !showServer;

    if (elements.importInput) {
        if (protocol === "auto") {
            elements.importInput.placeholder = "示例（自动选择，按 Graph→IMAP→POP3 顺序尝试）：\nabc@hotmail.com----123456----clientidxxxx----refresh_token_xxx\nabc2@hotmail.com----654321（仅 IMAP/POP3 时用）";
        } else if (protocol === "graph") {
            elements.importInput.placeholder = "示例：\nabc@hotmail.com----123456----clientidxxxx----refresh_token_xxx";
        } else if (protocol === "imap") {
            elements.importInput.placeholder = "示例（IMAP，邮箱----密码）：\nabc@hotmail.com----123456\nabc2@hotmail.com----654321";
        } else {
            elements.importInput.placeholder = "示例（POP3，邮箱----密码）：\nabc@hotmail.com----123456";
        }
    }

    if (elements.importServerInput) {
        const defaultHost = protocol === "imap" ? "outlook.office365.com:993" : "outlook.office365.com:995";
        if (!elements.importServerInput.value.trim()) {
            elements.importServerInput.placeholder = `留空使用默认：${defaultHost}`;
        }
    }
}

function handleImportFileSelect(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) {
        return;
    }
    // 重置 input 以便同一文件可重复选择
    event.target.value = "";
    // 选择文件后直接自动导入,不再要求用户再点一次"开始导入"
    importFromFile(file);
}

// 统一的文件导入入口:读取文件内容 → 填入 textarea → 自动触发导入
async function importFromFile(file) {
    // 读取文件内容
    let content;
    try {
        content = await readFileAsText(file);
    } catch (error) {
        setMessage(elements.importMessage, "文件读取失败，请重试", true);
        return;
    }

    const lineCount = content.split("\n").length;
    // 把内容填入 textarea,让用户看到导入了什么(导入失败时方便手动修改后重试)
    elements.importInput.value = content;
    setMessage(elements.importMessage, `正在从 ${file.name} 导入（${lineCount} 行）…`, false);

    // 自动触发导入
    await importAccounts();
}

function readFileAsText(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = (e) => resolve(String(e.target?.result || ""));
        reader.onerror = () => reject(new Error("read error"));
        reader.readAsText(file, "utf-8");
    });
}

// ─── 拖拽导入 ─────────────────────────────────────────
function handleImportDragOver(event) {
    event.preventDefault();
    event.stopPropagation();
    // 仅在拖入文件时显示拖拽反馈
    if (event.dataTransfer.types && Array.from(event.dataTransfer.types).includes("Files")) {
        event.dataTransfer.dropEffect = "copy";
        if (elements.importPanel) {
            elements.importPanel.classList.add("is-drag-over");
        }
    }
}

function handleImportDragLeave(event) {
    event.preventDefault();
    event.stopPropagation();
    // relatedTarget 为 null 或不在 panel 内时,才移除高亮(避免子元素间切换反复触发)
    const related = event.relatedTarget;
    if (!related || !elements.importPanel || !elements.importPanel.contains(related)) {
        if (elements.importPanel) {
            elements.importPanel.classList.remove("is-drag-over");
        }
    }
}

async function handleImportDrop(event) {
    event.preventDefault();
    event.stopPropagation();
    if (elements.importPanel) {
        elements.importPanel.classList.remove("is-drag-over");
    }

    const files = event.dataTransfer && event.dataTransfer.files;
    if (!files || files.length === 0) return;

    const file = files[0];
    // 检查文件类型(.txt / .csv)
    const fileName = (file.name || "").toLowerCase();
    const isTextFile = fileName.endsWith(".txt") || fileName.endsWith(".csv")
        || file.type === "text/plain" || file.type === "text/csv" || file.type === "";
    if (!isTextFile) {
        setMessage(elements.importMessage, "仅支持 .txt 或 .csv 文件", true);
        return;
    }

    await importFromFile(file);
}

async function exportAccounts() {
    setMessage(elements.accountMessage, text.exportingAccounts, false);
    elements.exportAccountsBtn.disabled = true;

    try {
        const response = await fetch("/api/accounts/export");

        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || text.requestFailed);
        }

        const blob = await response.blob();
        const downloadUrl = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = downloadUrl;
        anchor.download = getDownloadFileNameFromDisposition(response.headers.get("content-disposition"));
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(downloadUrl);

        setMessage(elements.accountMessage, text.exportAccountsSuccess, false);
    } catch (error) {
        setMessage(elements.accountMessage, error.message || text.requestFailed, true);
    } finally {
        elements.exportAccountsBtn.disabled = false;
    }
}

async function saveTags() {
    if (!state.selectedAccountId) {
        setMessage(elements.accountMessage, text.chooseAccountFirst, true);
        return;
    }

    try {
        await persistTags(state.selectedAccountId, elements.tagsInput.value);
        setMessage(elements.accountMessage, text.saveTagsSuccess, false);
    } catch (error) {
        setMessage(elements.accountMessage, error.message, true);
    }
}

async function saveRemark() {
    if (!state.selectedAccountId) {
        setMessage(elements.accountMessage, text.chooseAccountFirst, true);
        return;
    }

    try {
        await persistRemark(state.selectedAccountId, elements.remarkInput.value);
        setMessage(elements.accountMessage, SAVE_REMARK_SUCCESS_TEXT, false);
    } catch (error) {
        setMessage(elements.accountMessage, error.message, true);
    }
}

async function persistTags(accountId, tags) {
    const data = await api(`/api/accounts/${accountId}/tags`, {
        method: "POST",
        body: JSON.stringify({ tags })
    });

    const account = state.accounts.find((item) => item.id === accountId);
    if (account) {
        account.tags = data.tags;
    }

    updateTagFilterOptions();

    if (state.selectedAccountId === accountId) {
        updateSelectedAccountSummary();
    } else {
        renderAccounts();
    }

    return data;
}

async function persistRemark(accountId, remark) {
    const data = await api(`/api/accounts/${accountId}/remark`, {
        method: "POST",
        body: JSON.stringify({ remark })
    });

    const account = state.accounts.find((item) => item.id === accountId);
    if (account) {
        account.remark = data.remark;
    }

    if (state.selectedAccountId === accountId) {
        updateSelectedAccountSummary();
    } else {
        renderAccounts();
    }

    return data;
}

function setRemarkModalLoading(isLoading) {
    elements.remarkModalSaveBtn.disabled = isLoading;
    elements.remarkModalCancelBtn.disabled = isLoading;
    elements.remarkModalCloseBtn.disabled = isLoading;
    elements.remarkModalInput.disabled = isLoading;
    elements.remarkModalSaveBtn.textContent = isLoading ? "保存中..." : "保存备注";
}

function openRemarkModal(account) {
    state.remarkModalAccountId = account.id;
    state.remarkModalReturnFocus = document.activeElement;

    elements.remarkModalEmail.textContent = account.email;
    elements.remarkModalCurrent.innerHTML = renderRemarkMarkup(account.remark);
    elements.remarkModalInput.value = account.remark || "";
    setMessage(elements.remarkModalMessage, "", false);
    setRemarkModalLoading(false);

    document.body.classList.add("modal-open");
    elements.remarkModal.hidden = false;
    window.requestAnimationFrame(() => {
        elements.remarkModal.classList.add("is-visible");
        elements.remarkModalInput.focus();
        elements.remarkModalInput.select();
    });
}

function closeRemarkModal(force = false) {
    if (!force && elements.remarkModalSaveBtn.disabled) {
        return;
    }

    elements.remarkModal.classList.remove("is-visible");
    document.body.classList.remove("modal-open");
    state.remarkModalAccountId = null;
    setMessage(elements.remarkModalMessage, "", false);

    window.setTimeout(() => {
        if (!elements.remarkModal.classList.contains("is-visible")) {
            elements.remarkModal.hidden = true;
        }
    }, 180);

    if (state.remarkModalReturnFocus && typeof state.remarkModalReturnFocus.focus === "function") {
        state.remarkModalReturnFocus.focus();
    }
    state.remarkModalReturnFocus = null;
}

function renderTagModalPreview(value) {
    elements.tagModalPreview.innerHTML = renderTagMarkup(value, text.tagPreviewEmpty, "tag tag-modal-chip");
}

function renderTagModalSuggestions(value) {
    const selectedTags = parseTags(value);
    const availableTags = getAllAvailableTags();

    if (!availableTags.length) {
        elements.tagModalSuggestions.innerHTML = '<span class="muted">暂无可用的历史标签</span>';
        return;
    }

    elements.tagModalSuggestions.replaceChildren(...availableTags.map((tag) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tag-suggestion-button";
        if (selectedTags.includes(tag)) {
            button.classList.add("is-active");
        }
        button.dataset.tag = tag;
        button.textContent = tag;
        return button;
    }));
}

function syncTagModalTagViews(value) {
    renderTagModalPreview(value);
    renderTagModalSuggestions(value);
}

function applyQuickTag(tag) {
    const nextTags = parseTags(elements.tagModalInput.value);
    if (!nextTags.includes(tag)) {
        nextTags.push(tag);
    }

    const normalizedValue = nextTags.join(", ");
    elements.tagModalInput.value = normalizedValue;
    syncTagModalTagViews(normalizedValue);
    elements.tagModalInput.focus();
}

function setTagModalLoading(isLoading) {
    elements.tagModalSaveBtn.disabled = isLoading;
    elements.tagModalCancelBtn.disabled = isLoading;
    elements.tagModalCloseBtn.disabled = isLoading;
    elements.tagModalInput.disabled = isLoading;
    elements.tagModalSaveBtn.textContent = isLoading ? text.savingTags : "保存标签";
}

function openTagModal(account) {
    state.tagModalAccountId = account.id;
    state.tagModalReturnFocus = document.activeElement;

    elements.tagModalEmail.textContent = account.email;
    elements.tagModalCurrentTags.innerHTML = renderTagMarkup(account.tags, text.tagCurrentEmpty);
    elements.tagModalInput.value = account.tags || "";
    syncTagModalTagViews(account.tags || "");
    setMessage(elements.tagModalMessage, "", false);
    setTagModalLoading(false);

    document.body.classList.add("modal-open");
    elements.tagModal.hidden = false;
    window.requestAnimationFrame(() => {
        elements.tagModal.classList.add("is-visible");
        elements.tagModalInput.focus();
        elements.tagModalInput.select();
    });
}

function closeTagModal(force = false) {
    if (!force && elements.tagModalSaveBtn.disabled) {
        return;
    }

    elements.tagModal.classList.remove("is-visible");
    document.body.classList.remove("modal-open");
    state.tagModalAccountId = null;
    setMessage(elements.tagModalMessage, "", false);

    window.setTimeout(() => {
        if (!elements.tagModal.classList.contains("is-visible")) {
            elements.tagModal.hidden = true;
        }
    }, 180);

    if (state.tagModalReturnFocus && typeof state.tagModalReturnFocus.focus === "function") {
        state.tagModalReturnFocus.focus();
    }
    state.tagModalReturnFocus = null;
}

function openSelectedAccountMails() {
    if (!state.selectedAccountId) {
        setMessage(elements.accountMessage, text.chooseAccountFirst, true);
        return;
    }

    switchView("mails");
    setMailMobileStep("list");
    loadMails();
    refreshMailTimerState();
}

async function promptAndSaveTags(accountId = state.selectedAccountId) {
    if (!accountId) {
        setMessage(elements.mailMessage, text.chooseAccountFirst, true);
        return;
    }

    const account = state.accounts.find((item) => item.id === accountId);
    if (!account) {
        setMessage(elements.mailMessage, text.chooseAccountFirst, true);
        return;
    }

    state.selectedAccountId = accountId;
    updateSelectedAccountSummary();
    openTagModal(account);
}

async function promptAndSaveRemark(accountId = state.selectedAccountId) {
    if (!accountId) {
        setMessage(elements.mailMessage, text.chooseAccountFirst, true);
        return;
    }

    const account = state.accounts.find((item) => item.id === accountId);
    if (!account) {
        setMessage(elements.mailMessage, text.chooseAccountFirst, true);
        return;
    }

    state.selectedAccountId = accountId;
    updateSelectedAccountSummary();
    openRemarkModal(account);
}

async function saveTagModalTags() {
    if (!state.tagModalAccountId) {
        closeTagModal(true);
        return;
    }

    const nextTags = normalizeTags(elements.tagModalInput.value);
    elements.tagModalInput.value = nextTags;
    syncTagModalTagViews(nextTags);
    setTagModalLoading(true);

    try {
        await persistTags(state.tagModalAccountId, nextTags);
        closeTagModal(true);
        setMessage(elements.mailMessage, text.saveTagsSuccess, false);
    } catch (error) {
        setMessage(elements.tagModalMessage, error.message, true);
        setTagModalLoading(false);
    }
}

async function saveRemarkModalRemark() {
    if (!state.remarkModalAccountId) {
        closeRemarkModal(true);
        return;
    }

    const nextRemark = elements.remarkModalInput.value.trim();
    elements.remarkModalInput.value = nextRemark;
    setRemarkModalLoading(true);

    try {
        await persistRemark(state.remarkModalAccountId, nextRemark);
        closeRemarkModal(true);
        setMessage(elements.mailMessage, SAVE_REMARK_SUCCESS_TEXT, false);
    } catch (error) {
        setMessage(elements.remarkModalMessage, error.message, true);
        setRemarkModalLoading(false);
    }
}

function handleGlobalKeydown(event) {
    if (!elements.tagModal.hidden) {
        if (event.key === "Escape") {
            event.preventDefault();
            closeTagModal();
            return;
        }

        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            saveTagModalTags();
            return;
        }
    }

    if (!elements.remarkModal.hidden) {
        if (event.key === "Escape") {
            event.preventDefault();
            closeRemarkModal();
            return;
        }

        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            saveRemarkModalRemark();
            return;
        }
    }

    if (!elements.deleteConfirmModal.hidden) {
        if (event.key === "Escape") {
            event.preventDefault();
            closeDeleteConfirmModal();
            return;
        }

        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            confirmDeleteAccounts();
        }
    }
}

async function copySelectedEmailAddress() {
    const account = getSelectedAccount();
    if (!account) {
        setMessage(elements.mailMessage, text.copyMailAddressUnavailable, true);
        return;
    }

    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(account.email);
        } else {
            const input = document.createElement("input");
            input.value = account.email;
            document.body.appendChild(input);
            input.select();
            document.execCommand("copy");
            input.remove();
        }
        setMessage(elements.mailMessage, text.copyMailAddressSuccess, false);
    } catch (error) {
        setMessage(elements.mailMessage, error.message || text.requestFailed, true);
    }
}

function toggleAutoRefresh() {
    state.autoRefreshEnabled = !state.autoRefreshEnabled;
    localStorage.setItem("mail_manager_auto_refresh", state.autoRefreshEnabled ? "1" : "0");
    startAutoRefresh();
    const message = state.autoRefreshEnabled
        ? text.autoRefreshEnabledHint
        : text.autoRefreshDisabledHint;
    const target = state.currentView === "mails" ? elements.mailMessage : elements.accountMessage;
    setMessage(target, message, false);
}

elements.importBtn.addEventListener("click", importAccounts);
elements.importFileInput.addEventListener("change", handleImportFileSelect);
// 拖拽导入:在整个导入面板上绑定事件
if (elements.importPanel) {
    elements.importPanel.addEventListener("dragover", handleImportDragOver);
    elements.importPanel.addEventListener("dragleave", handleImportDragLeave);
    elements.importPanel.addEventListener("drop", handleImportDrop);
}
if (elements.importProtocolSelect) {
    elements.importProtocolSelect.addEventListener("change", updateImportProtocolVisibility);
}
elements.exportAccountsBtn.addEventListener("click", exportAccounts);
elements.refreshAccountsBtn.addEventListener("click", () => loadAccounts());
elements.refreshLogRunBtn.addEventListener("click", triggerTokenRefreshTask);
elements.saveTagsBtn.addEventListener("click", saveTags);
elements.saveRemarkBtn.addEventListener("click", saveRemark);
elements.openMailsBtn.addEventListener("click", openSelectedAccountMails);
elements.copyMailAddressBtn.addEventListener("click", copySelectedEmailAddress);
elements.toggleAutoRefreshBtn.addEventListener("click", toggleAutoRefresh);
if (elements.forceRefreshMailsBtn) {
    elements.forceRefreshMailsBtn.addEventListener("click", forceRefreshMails);
}
elements.remarkModalBackdrop.addEventListener("click", closeRemarkModal);
elements.remarkModalCancelBtn.addEventListener("click", closeRemarkModal);
elements.remarkModalCloseBtn.addEventListener("click", closeRemarkModal);
elements.remarkModalSaveBtn.addEventListener("click", saveRemarkModalRemark);
elements.tagModalBackdrop.addEventListener("click", closeTagModal);
elements.tagModalCancelBtn.addEventListener("click", closeTagModal);
elements.tagModalCloseBtn.addEventListener("click", closeTagModal);
elements.tagModalInput.addEventListener("input", () => syncTagModalTagViews(elements.tagModalInput.value));
elements.tagModalSuggestions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-tag]");
    if (!button) {
        return;
    }
    applyQuickTag(button.dataset.tag);
});
elements.tagModalSaveBtn.addEventListener("click", saveTagModalTags);
elements.searchInput.addEventListener("input", renderAccounts);
elements.mailSearchInput.addEventListener("input", renderAccounts);
elements.tagFilter.addEventListener("change", renderAccounts);
elements.mailTagFilter.addEventListener("change", renderAccounts);

// ─── 批量删除 / 单条删除 事件绑定 ───────────────────
if (elements.accountsBulkSelectAll) {
    elements.accountsBulkSelectAll.addEventListener("change", () => toggleSelectAll("accounts"));
}
if (elements.mailsBulkSelectAll) {
    elements.mailsBulkSelectAll.addEventListener("change", () => toggleSelectAll("mails"));
}
if (elements.accountsBulkDeleteBtn) {
    elements.accountsBulkDeleteBtn.addEventListener("click", () => {
        if (state.selectedAccountIds.size === 0) return;
        openDeleteConfirmModal(Array.from(state.selectedAccountIds));
    });
}
if (elements.mailsBulkDeleteBtn) {
    elements.mailsBulkDeleteBtn.addEventListener("click", () => {
        if (state.selectedAccountIds.size === 0) return;
        openDeleteConfirmModal(Array.from(state.selectedAccountIds));
    });
}
if (elements.accountsBulkRefreshBtn) {
    elements.accountsBulkRefreshBtn.addEventListener("click", refreshSelectedAccounts);
}
if (elements.mailsBulkRefreshBtn) {
    elements.mailsBulkRefreshBtn.addEventListener("click", refreshSelectedAccounts);
}
if (elements.deleteAccountBtn) {
    elements.deleteAccountBtn.addEventListener("click", () => {
        if (!state.selectedAccountId) return;
        deleteSingleAccount(state.selectedAccountId);
    });
}
if (elements.deleteConfirmBackdrop) {
    elements.deleteConfirmBackdrop.addEventListener("click", closeDeleteConfirmModal);
}
if (elements.deleteConfirmCloseBtn) {
    elements.deleteConfirmCloseBtn.addEventListener("click", closeDeleteConfirmModal);
}
if (elements.deleteConfirmCancelBtn) {
    elements.deleteConfirmCancelBtn.addEventListener("click", closeDeleteConfirmModal);
}
if (elements.deleteConfirmSaveBtn) {
    elements.deleteConfirmSaveBtn.addEventListener("click", confirmDeleteAccounts);
}
elements.refreshLogPage.addEventListener("change", async () => {
    state.logsPagination.page = Number(elements.refreshLogPage.value) || 1;
    await loadTokenRefreshLogs();
});
elements.refreshLogPageSize.addEventListener("change", async () => {
    state.logsPagination.pageSize = Number(elements.refreshLogPageSize.value) || DEFAULT_LOG_PAGE_SIZE;
    state.logsPagination.page = 1;
    await loadTokenRefreshLogs();
});
elements.refreshLogList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-preview-log-id]");
    if (!button) {
        return;
    }
    openRefreshLogPreview(Number(button.dataset.previewLogId));
});
elements.mailStepButtons.forEach((button) => {
    button.addEventListener("click", () => {
        setMailMobileStep(button.dataset.mailStepTarget);
    });
});
elements.menuItems.forEach((item) => {
    item.addEventListener("click", () => {
        switchView(item.dataset.view);
        if (
            item.dataset.view === "mails"
            && state.selectedAccountId
            && (
                state.loadedMailAccountId !== state.selectedAccountId
                || state.loadedMailFolder !== state.selectedFolder
            )
        ) {
            loadMails();
        }
        if (item.dataset.view === "logs") {
            loadTokenRefreshLogs();
        }
        if (item.dataset.view === "api") {
            loadApiKeys();
        }
        if (item.dataset.view === "proxy") {
            loadProxies();
        }
        refreshMailTimerState();
    });
});
elements.tabs.forEach((tab) => {
    tab.addEventListener("click", async () => {
        state.selectedFolder = tab.dataset.folder;
        elements.tabs.forEach((item) => item.classList.toggle("is-active", item === tab));
        state.selectedMailId = null;
        await loadMails();
    });
});
window.addEventListener("keydown", handleGlobalKeydown);
window.addEventListener("resize", updateMailMobileView);
window.addEventListener("beforeunload", stopAutoRefresh);

// ── API Key 管理 ──

function formatApiDateTime(timestamp) {
    if (!timestamp) {
        return text.apiNeverUsed;
    }
    return formatDateTime(timestamp);
}

function setApiCreateLoading(isLoading) {
    elements.apiKeyCreateBtn.disabled = isLoading;
    elements.apiKeyNameInput.disabled = isLoading;
    elements.apiKeyCreateBtn.textContent = isLoading ? text.apiCreating : text.apiCreate;
}

async function loadApiKeys() {
    try {
        const data = await api("/api/keys");
        state.apiKeys = data.items || [];
        renderApiKeys();
    } catch (error) {
        setMessage(elements.apiMessage, error.message, true);
    }
}

function renderApiKeys() {
    if (!state.apiKeys.length) {
        elements.apiKeyList.innerHTML = `<div class="empty-state">${text.apiEmpty}</div>`;
        return;
    }

    elements.apiKeyList.innerHTML = state.apiKeys.map((apiKey) => `
        <div class="api-key-card">
            <div class="api-key-card-head">
                <span class="api-key-name">${escapeHtml(apiKey.name || "未命名")}</span>
                <button class="api-key-delete-btn" type="button" data-key-id="${apiKey.id}">${text.apiDelete}</button>
            </div>
            <div class="api-key-value-row">
                <span class="api-key-value">${escapeHtml(apiKey.key)}</span>
                <button class="api-key-copy-btn" type="button" data-copy-key="${escapeHtml(apiKey.key)}">复制</button>
            </div>
            <div class="api-key-meta">
                <span>创建于 ${formatDateTime(apiKey.created_at)}</span>
                <span>${text.apiLastUsed}: ${formatApiDateTime(apiKey.last_used_at)}</span>
            </div>
        </div>
    `).join("");

    // 复制按钮
    elements.apiKeyList.querySelectorAll(".api-key-copy-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const keyValue = btn.dataset.copyKey;
            try {
                await navigator.clipboard.writeText(keyValue);
                btn.textContent = text.apiCopied;
                btn.classList.add("copied");
                setTimeout(() => {
                    btn.textContent = "复制";
                    btn.classList.remove("copied");
                }, 2000);
            } catch {
                // fallback
                const input = document.createElement("input");
                input.value = keyValue;
                document.body.appendChild(input);
                input.select();
                document.execCommand("copy");
                input.remove();
                btn.textContent = text.apiCopied;
                btn.classList.add("copied");
                setTimeout(() => {
                    btn.textContent = "复制";
                    btn.classList.remove("copied");
                }, 2000);
            }
        });
    });

    // 删除按钮
    elements.apiKeyList.querySelectorAll(".api-key-delete-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            if (!confirm(text.apiDeleteConfirm)) {
                return;
            }
            try {
                await api(`/api/keys/${btn.dataset.keyId}`, { method: "DELETE" });
                await loadApiKeys();
            } catch (error) {
                setMessage(elements.apiMessage, error.message, true);
            }
        });
    });
}

async function createApiKey() {
    const name = elements.apiKeyNameInput.value.trim();
    if (!name) {
        setMessage(elements.apiMessage, "请输入 Key 名称", true);
        return;
    }

    setApiCreateLoading(true);
    setMessage(elements.apiMessage, "", false);

    try {
        await api("/api/keys", {
            method: "POST",
            body: JSON.stringify({ name })
        });
        elements.apiKeyNameInput.value = "";
        setMessage(elements.apiMessage, text.apiCreateSuccess, false);
        await loadApiKeys();
    } catch (error) {
        setMessage(elements.apiMessage, error.message, true);
    } finally {
        setApiCreateLoading(false);
    }
}

elements.apiKeyCreateBtn.addEventListener("click", createApiKey);
elements.apiKeyNameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
        event.preventDefault();
        createApiKey();
    }
});

// ── 接口文档：自动显示服务地址 + 点击复制完整 URL ──

(function initApiDoc() {
    const baseUrlEl = document.getElementById("api-base-url");
    const copyBtn = document.getElementById("api-base-url-copy-btn");
    if (!baseUrlEl) return;

    // 自动获取当前服务地址
    const baseUrl = window.location.origin;
    baseUrlEl.textContent = baseUrl;

    // 复制服务地址
    if (copyBtn) {
        copyBtn.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(baseUrl);
            } catch { /* ignore */ }
            copyBtn.textContent = text.apiCopied;
            copyBtn.classList.add("copied");
            setTimeout(() => {
                copyBtn.textContent = "复制";
                copyBtn.classList.remove("copied");
            }, 2000);
        });
    }

    // 点击接口路径 → 复制完整 URL
    document.querySelectorAll(".api-doc-path").forEach((el) => {
        el.title = `点击复制完整地址: ${baseUrl}${el.dataset.path}`;
        el.addEventListener("click", async () => {
            const fullUrl = baseUrl + el.dataset.path;
            try {
                await navigator.clipboard.writeText(fullUrl);
            } catch {
                const input = document.createElement("input");
                input.value = fullUrl;
                document.body.appendChild(input);
                input.select();
                document.execCommand("copy");
                input.remove();
            }
            // 短暂视觉反馈
            el.style.background = "rgba(9, 105, 218, 0.12)";
            el.style.color = "#0550ae";
            setTimeout(() => {
                el.style.background = "";
                el.style.color = "";
            }, 600);
        });
    });
})();

// ── 代理池管理 ──

async function loadProxies() {
    try {
        const data = await api("/api/proxies");
        state.proxies = data.items || [];
        renderProxies();
    } catch (error) {
        setMessage(elements.proxyMessage, error.message, true);
    }
}

function renderProxies() {
    if (!state.proxies.length) {
        elements.proxyList.innerHTML = '<div class="empty-state">暂无代理，请先添加</div>';
        elements.proxyCount.textContent = "0";
        elements.proxyAvailableCount.textContent = "0";
        return;
    }

    const total = state.proxies.length;
    const available = state.proxies.filter((p) => p.status === 1).length;
    elements.proxyCount.textContent = String(total);
    elements.proxyAvailableCount.textContent = String(available);

    elements.proxyList.innerHTML = state.proxies.map((p) => {
        const statusLabel = p.status === 1 ? "正常" : "失效";
        const statusClass = p.status === 1 ? "online" : "offline";
        const toggleLabel = p.status === 1 ? "标记失效" : "标记可用";
        const typeBadge = p.proxy_type.toUpperCase();
        const authHint = p.username ? `${p.username}:***@` : "";

        // 延迟显示
        let latencyHtml = "";
        if (p.latency_ms > 0) {
            let latencyColor = "latency-high";
            if (p.latency_ms < 300) latencyColor = "latency-low";
            else if (p.latency_ms < 800) latencyColor = "latency-mid";
            latencyHtml = `<span class="proxy-latency ${latencyColor}">${p.latency_ms}ms</span>`;
        }

        // 出口 IP
        let ipHtml = "";
        if (p.exit_ip) {
            ipHtml = `<span class="proxy-exit-ip" title="点击复制" data-copy="${p.exit_ip}">${escapeHtml(p.exit_ip)}</span>`;
        }

        // 纯净度
        let purityHtml = "";
        let purityLevel = "";
        try {
            const purity = JSON.parse(p.purity_info || "{}");
            if (purity.level && purity.label) {
                const levelColor = {"高":"purity-high","中":"purity-mid","低":"purity-low"}[purity.level] || "purity-mid";
                const location = [purity.country, purity.city].filter(Boolean).join(" ");
                purityLevel = purity.level;
                purityHtml = `
                    <span class="proxy-purity ${levelColor}">${purity.level}纯净 · ${escapeHtml(purity.label)}</span>
                    ${location ? `<span class="proxy-location">${escapeHtml(location)}</span>` : ""}
                    ${purity.isp ? `<span class="proxy-isp">${escapeHtml(purity.isp)}</span>` : ""}
                `;
            } else if (p.exit_ip) {
                purityHtml = '<span class="proxy-purity purity-unknown">点击"检测全部"获取纯净度</span>';
            } else {
                purityHtml = '<span class="proxy-purity purity-unknown">未获取出口IP，检查代理类型是否正确</span>';
            }
        } catch(e) {
            purityHtml = '<span class="proxy-purity purity-unknown">未获取出口IP，检查代理类型是否正确</span>';
        }

        // 类型切换
        const otherType = p.proxy_type === "http" ? "socks5" : "http";
        const switchTypeLabel = `切换到${otherType.toUpperCase()}`;

        return `
            <div class="proxy-card">
                <div class="proxy-card-head">
                    <div class="proxy-card-title">
                        <div class="proxy-card-title-row">
                            ${escapeHtml(p.name)}
                            <span class="proxy-status-badge ${statusClass}">${statusLabel}</span>
                        </div>
                        <div class="proxy-card-purities">${purityHtml}</div>
                    </div>
                    <div class="proxy-card-actions">
                        <button class="proxy-type-switch-btn" type="button" data-proxy-id="${p.id}" data-new-type="${otherType}">${switchTypeLabel}</button>
                        <button class="proxy-toggle-btn" type="button" data-proxy-id="${p.id}" data-new-status="${p.status === 1 ? 0 : 1}">${toggleLabel}</button>
                        <button class="proxy-delete-btn" type="button" data-proxy-id="${p.id}">${text.proxyDelete}</button>
                    </div>
                </div>
                <div class="proxy-card-detail">
                    <span>${typeBadge} ${authHint}${escapeHtml(p.host)}:${p.port}</span>
                    ${latencyHtml}
                    ${ipHtml}
                    <span>使用 ${p.use_count || 0} 次</span>
                    ${p.last_checked_at ? `<span>检测于 ${formatDateTime(p.last_checked_at)}</span>` : ""}
                </div>
            </div>
        `;
    }).join("");

    elements.proxyList.querySelectorAll(".proxy-delete-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            try {
                await api(`/api/proxies/${btn.dataset.proxyId}`, { method: "DELETE" });
                await loadProxies();
            } catch (error) {
                setMessage(elements.proxyMessage, error.message, true);
            }
        });
    });

    elements.proxyList.querySelectorAll(".proxy-toggle-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const newStatus = parseInt(btn.dataset.newStatus);
            try {
                await api(`/api/proxies/${btn.dataset.proxyId}/status?status=${newStatus}`, { method: "POST" });
                await loadProxies();
            } catch (error) {
                setMessage(elements.proxyMessage, error.message, true);
            }
        });
    });

    // 点击出口 IP 复制
    elements.proxyList.querySelectorAll(".proxy-exit-ip").forEach((el) => {
        el.addEventListener("click", async () => {
            const ip = el.dataset.copy;
            try { await navigator.clipboard.writeText(ip); } catch(e) {}
            const orig = el.textContent;
            el.textContent = "已复制";
            setTimeout(() => { el.textContent = orig; }, 1500);
        });
    });

    // 切换代理类型
    elements.proxyList.querySelectorAll(".proxy-type-switch-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            try {
                await api(`/api/proxies/${btn.dataset.proxyId}/type?proxy_type=${btn.dataset.newType}`, { method: "POST" });
                await loadProxies();
                setMessage(elements.proxyMessage, `类型已切换为 ${btn.dataset.newType.toUpperCase()}，请重新检测`, false);
            } catch (error) {
                setMessage(elements.proxyMessage, error.message, true);
            }
        });
    });
}

async function importProxies() {
    const value = elements.proxyImportInput.value.trim();
    if (!value) {
        setMessage(elements.proxyImportMessage, "请先粘贴代理配置", true);
        return;
    }

    elements.proxyImportBtn.disabled = true;
    try {
        const data = await api("/api/proxies/import", {
            method: "POST",
            body: JSON.stringify({ text: value })
        });
        setMessage(elements.proxyImportMessage,
            `导入完成：新增 ${data.inserted}，跳过 ${data.skipped}（重复或格式错误）`,
            false
        );
        elements.proxyImportInput.value = "";
        await loadProxies();
        // 自动检测新代理
        await checkAllProxies();
    } catch (error) {
        setMessage(elements.proxyImportMessage, error.message, true);
    } finally {
        elements.proxyImportBtn.disabled = false;
    }
}

async function checkAllProxies() {
    elements.proxyCheckBtn.disabled = true;
    elements.proxyCheckBtn.textContent = text.proxyChecking;
    setMessage(elements.proxyMessage, "正在检测代理可用性...", false);
    try {
        const data = await api("/api/proxies/check", { method: "POST" });
        setMessage(elements.proxyMessage,
            `检测完成：${data.available}/${data.total} 个可用`,
            false
        );
        await loadProxies();
    } catch (error) {
        setMessage(elements.proxyMessage, error.message, true);
    } finally {
        elements.proxyCheckBtn.disabled = false;
        elements.proxyCheckBtn.textContent = text.proxyCheckAll;
    }
}

elements.proxyImportBtn.addEventListener("click", importProxies);
elements.proxyCheckBtn.addEventListener("click", checkAllProxies);

// 手动添加代理
elements.proxyAddBtn.addEventListener("click", () => {
    elements.proxyAddForm.hidden = false;
    elements.proxyAddHost.focus();
});

elements.proxyAddCancelBtn.addEventListener("click", () => {
    elements.proxyAddForm.hidden = true;
    elements.proxyAddHost.value = "";
    elements.proxyAddPort.value = "1080";
    elements.proxyAddUser.value = "";
    elements.proxyAddPass.value = "";
});

elements.proxyAddSaveBtn.addEventListener("click", async () => {
    const host = elements.proxyAddHost.value.trim();
    const port = parseInt(elements.proxyAddPort.value) || 0;
    if (!host || port < 1 || port > 65535) {
        setMessage(elements.proxyMessage, "请填写有效的地址和端口", true);
        return;
    }

    const params = new URLSearchParams({
        host: host,
        port: String(port),
        proxy_type: elements.proxyAddType.value,
        username: elements.proxyAddUser.value.trim(),
        password: elements.proxyAddPass.value.trim()
    });

    elements.proxyAddSaveBtn.disabled = true;
    try {
        await api(`/api/proxies?${params}`, { method: "POST" });
        elements.proxyAddCancelBtn.click(); // 关闭表单
        await loadProxies();
        setMessage(elements.proxyMessage, "代理已添加", false);
    } catch (error) {
        setMessage(elements.proxyMessage, error.message, true);
    } finally {
        elements.proxyAddSaveBtn.disabled = false;
    }
});

updateAutoRefreshUi();
updateViewActions();
updateMailActions();
updateMailMobileView();
setRefreshLogRunLoading(false);
renderRefreshLogs();
updateImportProtocolVisibility();

// 把所有原生 <select> 替换为自定义下拉
["import-protocol-select", "import-ssl-select", "tag-filter", "mail-tag-filter", "refresh-log-page", "refresh-log-page-size", "proxy-add-type"]
    .forEach((id) => {
        const el = document.getElementById(id);
        if (el) enhanceSelect(el);
    });
// 如果上次开启了自动刷新，页面加载后立即启动
if (state.autoRefreshEnabled) {
    startAutoRefresh();
}
loadAccounts();

// ─── 设置页面:版本检查 ─────────────────────────────────
async function loadVersionInfo() {
    try {
        const data = await api("/api/version");
        if (elements.settingsCurrentVersion) {
            elements.settingsCurrentVersion.textContent = data.version || "—";
        }
        if (elements.settingsGithubRepoInput) {
            elements.settingsGithubRepoInput.value = data.github_repo || "";
        }
    } catch (error) {
        if (elements.settingsCurrentVersion) {
            elements.settingsCurrentVersion.textContent = "—";
        }
    }
}

async function saveGithubRepo() {
    const repo = (elements.settingsGithubRepoInput.value || "").trim();
    setMessage(elements.settingsRepoMessage, "", false);
    elements.settingsSaveRepoBtn.disabled = true;
    elements.settingsSaveRepoBtn.textContent = "保存中…";
    try {
        await api("/api/settings", {
            method: "POST",
            body: JSON.stringify({ github_repo: repo })
        });
        setMessage(elements.settingsRepoMessage, text.settingsRepoSaved, false);
    } catch (error) {
        setMessage(elements.settingsRepoMessage, error.message, true);
    } finally {
        elements.settingsSaveRepoBtn.disabled = false;
        elements.settingsSaveRepoBtn.textContent = "保存仓库地址";
    }
}

async function checkForUpdate() {
    setMessage(elements.settingsUpdateMessage, "", false);
    if (elements.settingsCheckStatus) {
        elements.settingsCheckStatus.textContent = text.settingsChecking;
    }
    elements.settingsCheckUpdateBtn.disabled = true;
    elements.settingsCheckUpdateBtn.textContent = text.settingsChecking;
    if (elements.settingsUpdateResult) {
        elements.settingsUpdateResult.hidden = true;
    }

    try {
        const data = await api("/api/check-update");

        if (data.error) {
            setMessage(elements.settingsUpdateMessage, data.error, true);
            if (elements.settingsCheckStatus) {
                elements.settingsCheckStatus.textContent = "";
            }
            return;
        }

        const hasUpdate = data.has_update === true;
        const latest = data.latest_version || "—";

        // 展示结果卡片
        if (elements.settingsUpdateResult) {
            elements.settingsUpdateResult.hidden = false;
        }
        if (elements.settingsLatestVersion) {
            elements.settingsLatestVersion.textContent = latest;
        }
        if (elements.settingsUpdateBadge) {
            elements.settingsUpdateBadge.hidden = !hasUpdate;
            elements.settingsUpdateBadge.textContent = hasUpdate ? "有新版本" : "已最新";
            elements.settingsUpdateBadge.className = hasUpdate
                ? "update-badge update-badge-new"
                : "update-badge update-badge-ok";
        }
        if (elements.settingsPublishedAt) {
            elements.settingsPublishedAt.textContent = data.published_at
                ? new Date(data.published_at).toLocaleString("zh-CN")
                : "—";
        }

        // 更新日志
        const notes = (data.release_notes || "").trim();
        if (elements.settingsReleaseNotesCard && elements.settingsReleaseNotes) {
            if (notes) {
                elements.settingsReleaseNotesCard.hidden = false;
                elements.settingsReleaseNotes.innerHTML = escapeHtml(notes).replace(/\n/g, "<br>");
            } else {
                elements.settingsReleaseNotesCard.hidden = true;
            }
        }

        // 下载链接
        if (elements.settingsReleaseLink) {
            if (data.release_url) {
                elements.settingsReleaseLink.hidden = false;
                elements.settingsReleaseLink.href = data.release_url;
            } else {
                elements.settingsReleaseLink.hidden = true;
            }
        }

        // 一键更新按钮:仅在有新版本时显示
        if (elements.settingsPerformUpdateBtn) {
            elements.settingsPerformUpdateBtn.hidden = !hasUpdate;
        }

        // 顶部消息
        const message = hasUpdate
            ? text.settingsHasUpdate(latest)
            : text.settingsUpToDate(latest);
        setMessage(elements.settingsUpdateMessage, message, !hasUpdate ? false : false);
        if (elements.settingsCheckStatus) {
            elements.settingsCheckStatus.textContent = "";
        }
    } catch (error) {
        setMessage(elements.settingsUpdateMessage, text.settingsCheckFailed(error.message), true);
        if (elements.settingsCheckStatus) {
            elements.settingsCheckStatus.textContent = "";
        }
    } finally {
        elements.settingsCheckUpdateBtn.disabled = false;
        elements.settingsCheckUpdateBtn.textContent = "立即检查更新";
    }
}

async function performUpdate() {
    if (!window.confirm("确认要自动下载并更新到最新版本吗？\n\n更新过程中请勿关闭页面，更新完成后需要手动重启服务。")) {
        return;
    }

    // 打开进度弹窗,重置 UI
    openUpdateProgressModal();

    try {
        const response = await fetch("/api/perform-update", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-API-Key": state.apiKey || ""
            }
        });

        if (!response.ok && !response.body) {
            const text = await response.text().catch(() => "");
            showUpdateError(`HTTP ${response.status}`, text || `服务端返回 ${response.status} 错误`, "");
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop();  // 保留最后一行(可能不完整)
            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed) continue;
                try {
                    const data = JSON.parse(trimmed);
                    handleUpdateProgressEvent(data);
                } catch (e) {
                    // 忽略无法解析的行
                }
            }
        }
        // 处理 buffer 中剩余的最后一行
        if (buffer.trim()) {
            try {
                const data = JSON.parse(buffer.trim());
                handleUpdateProgressEvent(data);
            } catch (e) {}
        }
    } catch (error) {
        showUpdateError("网络错误", error.message || String(error),
            "无法连接到服务端。可能原因：\n1) 服务已停止运行 → 请重启服务\n2) 网络问题 → 检查网络连接");
    }
}

function openUpdateProgressModal() {
    // 重置所有 UI 元素
    if (elements.updateProgressFill) elements.updateProgressFill.style.width = "0%";
    if (elements.updateProgressPercent) elements.updateProgressPercent.textContent = "0%";
    if (elements.updateProgressStage) elements.updateProgressStage.textContent = "准备中…";
    if (elements.updateProgressTitle) elements.updateProgressTitle.textContent = "正在更新";
    if (elements.updateProgressSubtitle) elements.updateProgressSubtitle.textContent = "请勿关闭页面";
    if (elements.updateProgressCloseBtn) elements.updateProgressCloseBtn.hidden = true;
    if (elements.updateProgressCancelBtn) {
        elements.updateProgressCancelBtn.textContent = "取消";
        elements.updateProgressCancelBtn.disabled = false;
    }
    if (elements.updateVersionInfo) elements.updateVersionInfo.hidden = true;
    if (elements.updateSkippedFiles) elements.updateSkippedFiles.hidden = true;
    if (elements.updateErrorBox) elements.updateErrorBox.hidden = true;
    if (elements.updateSuccessBox) elements.updateSuccessBox.hidden = true;

    if (elements.updateProgressModal) {
        elements.updateProgressModal.hidden = false;
        void elements.updateProgressModal.offsetWidth;
        elements.updateProgressModal.classList.add("is-visible");
        document.body.classList.add("modal-open");
    }
}

function closeUpdateProgressModal() {
    if (elements.updateProgressModal) {
        elements.updateProgressModal.classList.remove("is-visible");
        document.body.classList.remove("modal-open");
        setTimeout(() => {
            if (elements.updateProgressModal) elements.updateProgressModal.hidden = true;
        }, 180);
    }
}

function handleUpdateProgressEvent(data) {
    const stage = data.stage || "";
    const message = data.message || "";
    const progress = typeof data.progress === "number" ? data.progress : null;

    // 更新进度条
    if (progress !== null) {
        if (elements.updateProgressFill) {
            elements.updateProgressFill.style.width = `${Math.min(100, Math.max(0, progress))}%`;
        }
        if (elements.updateProgressPercent) {
            elements.updateProgressPercent.textContent = `${progress}%`;
        }
    }

    // 更新阶段文字
    if (message && elements.updateProgressStage) {
        elements.updateProgressStage.textContent = message;
    }

    // 版本信息
    if (data.latest_version && data.current_version) {
        if (elements.updateVersionInfo) elements.updateVersionInfo.hidden = false;
        if (elements.updateVersionChange) {
            elements.updateVersionChange.textContent = `${data.current_version} → ${data.latest_version}`;
        }
    }

    if (stage === "done") {
        // 更新完成(done 之后会紧跟 restarting 事件)
        if (elements.updateProgressStage) {
            elements.updateProgressStage.textContent = message || "更新完成，正在准备重启服务…";
        }
        if (elements.updateVersionInfo && data.latest_version && data.current_version) {
            elements.updateVersionInfo.hidden = false;
            if (elements.updateVersionChange) {
                elements.updateVersionChange.textContent = `${data.current_version} → ${data.latest_version}`;
            }
        }
        if (data.skipped_files && data.skipped_files.length > 0) {
            if (elements.updateSkippedFiles) elements.updateSkippedFiles.hidden = false;
            if (elements.updateSkippedFilesList) {
                elements.updateSkippedFilesList.innerHTML = data.skipped_files
                    .map((f) => `<div class="skipped-file-item">${escapeHtml(String(f))}</div>`)
                    .join("");
            }
        }
        if (elements.settingsPerformUpdateBtn) elements.settingsPerformUpdateBtn.hidden = true;
    } else if (stage === "restarting") {
        // 服务正在重启
        if (elements.updateProgressTitle) elements.updateProgressTitle.textContent = "正在重启服务";
        if (elements.updateProgressSubtitle) elements.updateProgressSubtitle.textContent = "服务将短暂不可用，页面会自动刷新";
        if (elements.updateProgressStage) {
            elements.updateProgressStage.innerHTML = '<span class="restart-spinner"></span> 服务正在重启，请等待页面自动刷新…';
        }
        if (elements.updateProgressFill) {
            elements.updateProgressFill.style.width = "100%";
        }
        if (elements.updateProgressPercent) {
            elements.updateProgressPercent.textContent = "⟳";
        }
        // 隐藏成功/错误区域,禁用关闭按钮(重启中不能关闭)
        if (elements.updateSuccessBox) elements.updateSuccessBox.hidden = true;
        if (elements.updateErrorBox) elements.updateErrorBox.hidden = true;
        if (elements.updateProgressCancelBtn) {
            elements.updateProgressCancelBtn.disabled = true;
            elements.updateProgressCancelBtn.textContent = "等待重启…";
        }
        // 开始轮询 /health,服务恢复后自动刷新
        pollForRestart();
    } else if (stage === "error") {
        // 更新失败
        showUpdateError(data.error_type || "未知错误", message, data.suggestion || "");
    }
}

function showUpdateError(errorType, message, suggestion) {
    if (elements.updateProgressTitle) elements.updateProgressTitle.textContent = "更新失败";
    if (elements.updateProgressSubtitle) elements.updateProgressSubtitle.textContent = "请查看下方错误信息和建议";
    if (elements.updateProgressFill) elements.updateProgressFill.style.width = "0%";
    if (elements.updateProgressFill) elements.updateProgressFill.classList.add("is-error");
    if (elements.updateProgressPercent) elements.updateProgressPercent.textContent = "!";
    if (elements.updateProgressCloseBtn) elements.updateProgressCloseBtn.hidden = false;
    if (elements.updateProgressCancelBtn) {
        elements.updateProgressCancelBtn.textContent = "关闭";
        elements.updateProgressCancelBtn.disabled = false;
    }
    if (elements.updateErrorBox) elements.updateErrorBox.hidden = false;
    if (elements.updateErrorMessage) {
        elements.updateErrorMessage.innerHTML =
            `<span class="error-type-tag">${escapeHtml(errorType)}</span>` +
            `<div class="error-detail">${escapeHtml(message)}</div>`;
    }
    if (elements.updateErrorSuggestion) {
        elements.updateErrorSuggestion.innerHTML = suggestion
            ? `<span class="suggestion-label">建议操作</span><div class="suggestion-text">${escapeHtml(suggestion).replace(/\n/g, "<br>")}</div>`
            : "";
    }
}

// ─── 轮询服务重启 ─────────────────────────────────────
// 服务重启后 /health 会先不可用(连接失败)再恢复,恢复后自动刷新页面
async function pollForRestart() {
    const maxAttempts = 60;  // 最多等 120 秒
    const interval = 2000;    // 每 2 秒轮询一次
    let connectedBefore = false;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, interval));
        try {
            const response = await fetch("/health", { method: "GET", cache: "no-store" });
            if (response.ok) {
                if (connectedBefore) {
                    // 第一次连接成功可能是旧进程还没退出,需要确认是"先断开再恢复"
                    // 但如果连续两次都成功,说明新进程已启动
                }
                // 先假设已恢复,但需要确认旧进程确实退出过
                // 简单策略:第一次成功后等 2 秒再确认一次
                if (!connectedBefore) {
                    connectedBefore = true;
                    // 等一下再确认
                    await new Promise((resolve) => setTimeout(resolve, 2000));
                    try {
                        const confirmResp = await fetch("/health", { method: "GET", cache: "no-store" });
                        if (confirmResp.ok) {
                            // 确认恢复,刷新页面
                            if (elements.updateProgressStage) {
                                elements.updateProgressStage.innerHTML = '<span class="restart-success">✓ 服务已重启，正在刷新页面…</span>';
                            }
                            if (elements.updateProgressCancelBtn) {
                                elements.updateProgressCancelBtn.disabled = false;
                                elements.updateProgressCancelBtn.textContent = "刷新页面";
                                elements.updateProgressCancelBtn.onclick = () => window.location.reload();
                            }
                            setTimeout(() => window.location.reload(), 1500);
                            return;
                        }
                    } catch (e) {
                        // 确认失败,继续等待
                        connectedBefore = false;
                    }
                }
            }
        } catch (e) {
            // 服务不可用(正在重启中),这是正常的
            connectedBefore = false;
            if (elements.updateProgressStage) {
                elements.updateProgressStage.innerHTML =
                    `<span class="restart-spinner"></span> 服务正在重启…（已等待 ${attempt * 2} 秒）`;
            }
        }
    }

    // 超时
    if (elements.updateProgressTitle) elements.updateProgressTitle.textContent = "重启超时";
    if (elements.updateProgressSubtitle) elements.updateProgressSubtitle.textContent = "服务可能未能自动重启";
    if (elements.updateProgressStage) {
        elements.updateProgressStage.innerHTML =
            '<span class="restart-warning">服务重启超时，请手动重启服务后刷新页面</span>';
    }
    if (elements.updateProgressCancelBtn) {
        elements.updateProgressCancelBtn.disabled = false;
        elements.updateProgressCancelBtn.textContent = "关闭";
        elements.updateProgressCancelBtn.onclick = null;
    }
}

// 设置页面事件绑定
if (elements.settingsSaveRepoBtn) {
    elements.settingsSaveRepoBtn.addEventListener("click", saveGithubRepo);
}
if (elements.settingsCheckUpdateBtn) {
    elements.settingsCheckUpdateBtn.addEventListener("click", checkForUpdate);
}
if (elements.settingsPerformUpdateBtn) {
    elements.settingsPerformUpdateBtn.addEventListener("click", performUpdate);
}
if (elements.updateProgressBackdrop) {
    elements.updateProgressBackdrop.addEventListener("click", closeUpdateProgressModal);
}
if (elements.updateProgressCloseBtn) {
    elements.updateProgressCloseBtn.addEventListener("click", closeUpdateProgressModal);
}
if (elements.updateProgressCancelBtn) {
    elements.updateProgressCancelBtn.addEventListener("click", () => {
        // 更新中不允许关闭(只能取消请求),完成后可以关闭
        if (elements.updateSuccessBox && !elements.updateSuccessBox.hidden) {
            closeUpdateProgressModal();
        } else if (elements.updateErrorBox && !elements.updateErrorBox.hidden) {
            closeUpdateProgressModal();
        } else {
            // 更新进行中,确认是否关闭
            if (window.confirm("更新正在进行中，确定要关闭吗？关闭后更新可能中断。")) {
                closeUpdateProgressModal();
            }
        }
    });
}
