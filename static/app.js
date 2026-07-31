// Market Advisor V4 - 前端交互逻辑（修复版）
// 修复：推荐列表 confidence_level/confidence_name/condition_match 为空导致页面空白
// 额外保护：renderRadarChart 空数组保护
// 新增：模拟交易功能

// ============ Tab 切换 ============
document.querySelectorAll('.nav-link[data-tab]').forEach(link => {
    link.addEventListener('click', e => {
        e.preventDefault();
        const tab = link.dataset.tab;
        switchTab(tab);
    });
});

function switchTab(tab) {
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    document.querySelector(`.nav-link[data-tab="${tab}"]`)?.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.getElementById(`tab-${tab}`)?.classList.add('active');
    if (tab === 'dashboard') loadDashboard();
    if (tab === 'reports') loadReports();
    if (tab === 'health') loadModelHealth();
    if (tab === 'signals') loadSignalsTab();
    if (tab === 'simulate') {
        // 设置默认日期范围
        const end = new Date();
        const start = new Date();
        start.setDate(start.getDate() - 180);
        document.getElementById('sim-start-date').value = start.toISOString().split('T')[0];
        document.getElementById('sim-end-date').value = end.toISOString().split('T')[0];
    }
}

// ============ 工具函数 ============
function getSmashColor(val) {
    if (val == null) return '';
    if (val < 3) return 'smash-low';
    if (val <= 5) return 'smash-mid';
    return 'smash-high';
}
function formatNumber(val, digits = 2) {
    if (val == null || val === '' || isNaN(val)) return '--';
    return Number(val).toFixed(digits);
}
function formatPercent(val) {
    if (val == null || isNaN(val)) return '--';
    return (val * 100).toFixed(1) + '%';
}
async function fetchJSON(url) {
    const res = await fetch(url);
    return res.json();
}
async function postJSON(url, data) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    return res.json();
}

// ============ 仪表盘 ============
let smashChartInstance = null;

async function loadDashboard() {
    try {
        const resp = await fetchJSON('/api/dashboard');
        if (!resp.success) return;
        const data = resp.data;
        const summary = data.summary || {};
        document.getElementById('dashboard-date').textContent = summary.date ? `数据日期: ${summary.date}` : '暂无数据';
        document.getElementById('card-limit-up').textContent = summary.limit_up_count ?? '--';
        document.getElementById('card-max-board').textContent = summary.max_continuous_boards ?? '--';
        const smashEl = document.getElementById('card-smash');
        const smashVal = summary.smash_coefficient;
        smashEl.textContent = formatNumber(smashVal);
        smashEl.className = 'card-value smash-value ' + getSmashColor(smashVal);
        document.getElementById('card-sentiment').textContent = formatNumber(summary.sentiment_score);
        document.getElementById('card-seal').textContent = formatNumber(summary.avg_seal_amount);
        const advice = summary.smash_trade_advice || summary.smash_advantage || '暂无建议';
        document.getElementById('trade-advice').textContent = advice;
        document.getElementById('cycle-phase').textContent = summary.cycle_phase || '未知';
        document.getElementById('smash-signal').textContent = summary.smash_signal || '--';
        renderSmashChart(data.smash_chart || []);
        renderPredictions(data.predictions || [], 'prediction-cards');
    } catch (e) { console.error('Dashboard load error:', e); }
}

function renderSmashChart(chartData) {
    const ctx = document.getElementById('smashChart');
    if (smashChartInstance) smashChartInstance.destroy();
    const labels = chartData.map(d => d.date);
    const values = chartData.map(d => d.value);
    const colors = values.map(v => { if (v < 3) return '#0ecb81'; if (v <= 5) return '#fcd535'; return '#f6465d'; });
    smashChartInstance = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets: [{ label: '砸盘系数', data: values, borderColor: '#f0b90b', backgroundColor: 'rgba(240,185,11,0.1)', pointBackgroundColor: colors, pointRadius: 6, pointHoverRadius: 8, tension: 0.3, fill: true, borderWidth: 2 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: (ctx) => { const item = chartData[ctx.dataIndex]; return item ? `最高连板: ${item.max_boards || '--'}` : ''; } } } }, scales: { x: { ticks: { color: '#9ca3af', maxRotation: 45 }, grid: { color: 'rgba(45,55,72,0.3)' } }, y: { ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } }
    });
}

function renderPredictions(predictions, containerId) {
    const container = document.getElementById(containerId);
    if (!predictions || (Array.isArray(predictions) && predictions.length === 0) || (typeof predictions === 'object' && !Array.isArray(predictions) && Object.keys(predictions).length === 0)) {
        container.innerHTML = '<div class="text-muted">暂无预测数据</div>'; return;
    }
    const typeNames = { 'limit_up_count': '涨停数量', 'max_continuous_boards': '最高连板', 'main_concept': '主线概念', 'sentiment_direction': '情绪方向', 'operation_advice': '操作建议', 'smash_prediction': '砸盘预测' };
    let items = [];
    if (typeof predictions === 'object' && !Array.isArray(predictions)) { items = Object.entries(predictions).map(([type, data]) => ({type, data})); }
    else if (Array.isArray(predictions)) { items = predictions.map(p => ({type: p.type || p.prediction_type || '', data: p})); }
    container.innerHTML = items.map(({type, data}) => {
        const pred = data.predicted || data.advice || data.value || '--';
        const conf = data.confidence || 0;
        const reason = data.reason || data.detail || '';
        return `<div class="col-6 col-md-4 col-lg-3"><div class="pred-card"><div class="pred-type">${typeNames[type] || type}</div><div class="pred-value">${pred}</div><div class="pred-confidence">置信度: ${formatNumber(conf * 100, 0)}%</div>${reason ? `<div class="pred-confidence" style="margin-top:2px;font-size:0.65rem">${reason.substring(0, 50)}${reason.length > 50 ? '...' : ''}</div>` : ''}</div></div>`;
    }).join('');
}

// ============ 获取数据 ============
async function runFetchData() {
    const btn = document.getElementById('btn-fetch-data');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 获取中...';
    document.getElementById('daily-progress').style.display = 'block';
    document.getElementById('daily-status-text').textContent = '正在获取数据...';
    document.getElementById('daily-progress-bar').style.width = '10%';
    document.getElementById('daily-spinner').style.display = 'inline-block';
    try {
        const resp = await postJSON('/api/fetch', {});
        if (!resp.success) throw new Error(resp.error || '获取失败');
        const taskId = resp.task_id;
        const timer = setInterval(async () => {
            try {
                const sr = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
                if (!sr.success) return;
                const d = sr.data;
                document.getElementById('daily-progress-bar').style.width = d.progress + '%';
                document.getElementById('daily-status-text').textContent = d.message;
                if (d.status === 'completed' || d.status === 'error') {
                    clearInterval(timer);
                    document.getElementById('daily-spinner').style.display = 'none';
                    btn.disabled = false; btn.innerHTML = '<i class="bi bi-cloud-download"></i> 获取今日数据';
                    if (d.status === 'completed') setTimeout(() => loadDashboard(), 1000);
                }
            } catch (e) { console.error(e); }
        }, 2000);
    } catch (e) {
        document.getElementById('daily-status-text').textContent = '❌ ' + e.message;
        btn.disabled = false; btn.innerHTML = '<i class="bi bi-cloud-download"></i> 获取今日数据';
    }
}

// ============ 每日分析 ============
let dailyPollTimer = null, sealPieInstance = null, conceptBarInstance = null;

async function runDailyAnalysis() {
    const btn = document.getElementById('btn-run-daily');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 执行中...';
    document.getElementById('daily-progress').style.display = 'block';
    document.getElementById('daily-results').style.display = 'none';
    try {
        const resp = await postJSON('/api/daily', {});
        if (resp.success) pollDailyStatus(resp.task_id);
        else { alert('启动失败'); resetDailyBtn(); }
    } catch (e) { alert('请求失败'); resetDailyBtn(); }
}
function pollDailyStatus(taskId) {
    dailyPollTimer = setInterval(async () => {
        const resp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
        if (!resp.success) return;
        const d = resp.data;
        document.getElementById('daily-progress-bar').style.width = d.progress + '%';
        document.getElementById('daily-status-text').textContent = d.message;
        if (d.status === 'completed') { clearInterval(dailyPollTimer); document.getElementById('daily-spinner').style.display = 'none'; resetDailyBtn(); if (d.result) showDailyResults(d.result); }
        else if (d.status === 'error') { clearInterval(dailyPollTimer); resetDailyBtn(); }
    }, 2000);
}
function resetDailyBtn() { const btn = document.getElementById('btn-run-daily'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> 执行每日分析'; }

function showDailyResults(result) {
    document.getElementById('daily-results').style.display = 'block';
    const analysis = result.analysis || {};
    const tiers = analysis.board_tiers || {};
    document.getElementById('board-tiers').innerHTML = Object.entries(tiers).map(([level, stocks]) => {
        const sl = Array.isArray(stocks) ? stocks.join('、') : (typeof stocks === 'string' ? stocks : JSON.stringify(stocks));
        return `<div class="tier-item"><span class="tier-level">${level}</span><span class="tier-stocks">${sl || '无'}</span></div>`;
    }).join('') || '<div class="text-muted">无数据</div>';
    renderSealPie(analysis.seal_quality || {});
    renderConceptBar(analysis.concept_heat || {});
    renderPredictions(result.predictions || [], 'daily-prediction-cards');
}

function renderSealPie(sealData) {
    const ctx = document.getElementById('sealPieChart');
    if (sealPieInstance) sealPieInstance.destroy();
    let labels = ['强封', '中封', '弱封'], values = [0, 0, 0];
    if (sealData.strong) values[0] = Array.isArray(sealData.strong) ? sealData.strong.length : (sealData.strong.count || sealData.strong);
    if (sealData.medium) values[1] = Array.isArray(sealData.medium) ? sealData.medium.length : (sealData.medium.count || sealData.medium);
    if (sealData.weak) values[2] = Array.isArray(sealData.weak) ? sealData.weak.length : (sealData.weak.count || sealData.weak);
    if (values.every(v => v === 0) && typeof sealData === 'object') {
        const keys = Object.keys(sealData);
        if (keys.length > 0) { labels = keys; values = keys.map(k => { const v = sealData[k]; return Array.isArray(v) ? v.length : (typeof v === 'object' ? (v.count || 0) : (v || 0)); }); }
    }
    sealPieInstance = new Chart(ctx, { type: 'doughnut', data: { labels, datasets: [{ data: values, backgroundColor: ['#0ecb81', '#fcd535', '#f6465d'], borderWidth: 0 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { color: '#9ca3af' } } } } });
}

function renderConceptBar(conceptData) {
    const ctx = document.getElementById('conceptBarChart');
    if (conceptBarInstance) conceptBarInstance.destroy();
    let entries = [];
    if (Array.isArray(conceptData)) entries = conceptData.map(c => ({ name: c.concept || c.name || '', count: c.count || 0 }));
    else if (typeof conceptData === 'object') entries = Object.entries(conceptData).map(([name, count]) => ({ name, count: typeof count === 'object' ? (count.count || 0) : count }));
    entries.sort((a, b) => b.count - a.count); entries = entries.slice(0, 10);
    conceptBarInstance = new Chart(ctx, { type: 'bar', data: { labels: entries.map(e => e.name), datasets: [{ label: '涨停数', data: entries.map(e => e.count), backgroundColor: 'rgba(240,185,11,0.6)', borderColor: '#f0b90b', borderWidth: 1, borderRadius: 4 }] }, options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } }, y: { ticks: { color: '#9ca3af', font: { size: 11 } }, grid: { display: false } } } } });
}

// ============ 回测 ============
let btPollTimer = null, backtestBarInstance = null;

async function runBacktest() {
    const btn = document.getElementById('btn-run-backtest');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 执行中...';
    const maxDays = parseInt(document.getElementById('backtest-days').value) || 30;
    document.getElementById('backtest-progress').style.display = 'block';
    document.getElementById('backtest-results').style.display = 'none';
    try {
        const resp = await postJSON('/api/backtest', { max_days: maxDays });
        if (resp.success) pollBacktestStatus(resp.task_id);
        else { alert('启动失败'); resetBtBtn(); }
    } catch (e) { alert('请求失败'); resetBtBtn(); }
}
function pollBacktestStatus(taskId) {
    btPollTimer = setInterval(async () => {
        const resp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
        if (!resp.success) return;
        const d = resp.data;
        document.getElementById('bt-progress-bar').style.width = d.progress + '%';
        document.getElementById('bt-status-text').textContent = d.message;
        if (d.status === 'completed') { clearInterval(btPollTimer); document.getElementById('bt-spinner').style.display = 'none'; resetBtBtn(); if (d.result) showBacktestResults(d.result); }
        else if (d.status === 'error') { clearInterval(btPollTimer); resetBtBtn(); }
    }, 3000);
}
function resetBtBtn() { const btn = document.getElementById('btn-run-backtest'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> 执行回测'; }

function showBacktestResults(result) {
    document.getElementById('backtest-results').style.display = 'block';
    document.getElementById('bt-total-days').textContent = result.total_days || '--';
    document.getElementById('bt-total-pred').textContent = result.total_predictions || '--';
    document.getElementById('bt-total-verif').textContent = result.total_verifications || '--';
    renderBacktestBar(result.results || []);
    if (result.report) document.getElementById('backtest-report').innerHTML = marked.parse(result.report);
}
function renderBacktestBar(results) {
    const ctx = document.getElementById('backtestBarChart');
    if (backtestBarInstance) backtestBarInstance.destroy();
    const labels = results.map(r => r.date);
    const values = results.map(r => r.avg_score || 0);
    backtestBarInstance = new Chart(ctx, { type: 'bar', data: { labels, datasets: [{ label: '平均验证得分', data: values, backgroundColor: values.map(v => v >= 0.7 ? 'rgba(14,203,129,0.7)' : v >= 0.4 ? 'rgba(252,213,53,0.7)' : 'rgba(246,70,93,0.7)'), borderRadius: 3 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#9ca3af', maxRotation: 45, font: { size: 9 } }, grid: { color: 'rgba(45,55,72,0.3)' } }, y: { min: 0, max: 1, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } } });
}

// ============ 报告 ============
async function loadReports() {
    try {
        const resp = await fetchJSON('/api/reports');
        if (!resp.success) return;
        const reports = resp.data;
        const container = document.getElementById('report-list');
        if (!reports || reports.length === 0) { container.innerHTML = '<div class="text-muted">暂无报告</div>'; return; }
        container.innerHTML = reports.map(r => `<div class="report-item" onclick="loadReportDetail('${r.date}', this)"><div><div class="report-date"><i class="bi bi-file-earmark-text text-gold"></i> ${r.date}</div><div class="report-meta">${r.modified}</div></div><div class="report-meta">${(r.size / 1024).toFixed(1)}KB</div></div>`).join('');
    } catch (e) { console.error(e); }
}
async function loadReportDetail(date, el) {
    document.querySelectorAll('.report-item').forEach(e => e.classList.remove('active'));
    if (el) el.classList.add('active');
    document.getElementById('report-title').textContent = `报告 - ${date}`;
    document.getElementById('report-content').innerHTML = '<div class="text-muted">加载中...</div>';
    try {
        const resp = await fetchJSON(`/api/reports/${date}`);
        if (!resp.success) { document.getElementById('report-content').innerHTML = `<div class="text-danger">${resp.error}</div>`; return; }
        document.getElementById('report-content').innerHTML = marked.parse(resp.data.content);
    } catch (e) { document.getElementById('report-content').innerHTML = `<div class="text-danger">加载失败</div>`; }
}

// ============ 模型健康度 ============
let weightChartInstance = null, credibilityChartInstance = null, weightHistInstance = null;
const factorLabels = { 'momentum_factor': '动量因子', 'continuation_factor': '晋级率因子', 'concept_heat_factor': '概念热度', 'seal_quality_factor': '封板质量', 'cycle_factor': '周期因子', 'dragon_factor': '龙头因子', 'volume_factor': '量能因子', 'breadth_factor': '宽度因子', 'smash_factor': '砸盘系数' };

async function loadModelHealth() {
    try {
        const resp = await fetchJSON('/api/model/health');
        if (!resp.success) return;
        const data = resp.data;
        const health = data.health || {};
        document.getElementById('health-status').textContent = health.status || '--';
        document.getElementById('health-credibility').textContent = formatNumber(health.avg_credibility, 3);
        document.getElementById('health-total').textContent = health.total_factors || '--';
        document.getElementById('health-low').textContent = health.low_credibility_count || 0;
        const factors = health.factors || [];
        const details = data.weight_details || [];
        renderWeightChart(factors); renderCredibilityChart(factors); renderSmashFactorDetail(details); renderWeightHistory(details);
        renderCorrectionLogs(data.correction_logs || []);
    } catch (e) { console.error(e); }
}

function renderWeightChart(factors) {
    const ctx = document.getElementById('weightChart');
    if (weightChartInstance) weightChartInstance.destroy();
    weightChartInstance = new Chart(ctx, { type: 'bar', data: { labels: factors.map(f => factorLabels[f.name] || f.name), datasets: [{ label: '权重', data: factors.map(f => f.weight), backgroundColor: factors.map(f => f.name === 'smash_factor' ? 'rgba(240,185,11,0.8)' : 'rgba(30,144,255,0.6)'), borderColor: factors.map(f => f.name === 'smash_factor' ? '#f0b90b' : '#1e90ff'), borderWidth: 1, borderRadius: 4 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { display: false } }, y: { min: 0, max: 1, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } } });
}
function renderCredibilityChart(factors) {
    const ctx = document.getElementById('credibilityChart');
    if (credibilityChartInstance) credibilityChartInstance.destroy();
    const values = factors.map(f => f.credibility);
    credibilityChartInstance = new Chart(ctx, { type: 'bar', data: { labels: factors.map(f => factorLabels[f.name] || f.name), datasets: [{ label: '可信度', data: values, backgroundColor: values.map(v => v >= 0.7 ? 'rgba(14,203,129,0.7)' : v >= 0.4 ? 'rgba(252,213,53,0.7)' : 'rgba(246,70,93,0.7)'), borderRadius: 4 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { display: false } }, y: { min: 0, max: 1, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } } });
}
function renderSmashFactorDetail(details) {
    const container = document.getElementById('smash-factor-detail');
    const smash = details.find(d => d.name === 'smash_factor');
    if (!smash) { container.innerHTML = '<div class="text-muted">smash_factor 未初始化</div>'; return; }
    const credColor = smash.credibility >= 0.7 ? 'var(--green)' : smash.credibility >= 0.4 ? 'var(--yellow)' : 'var(--red)';
    container.innerHTML = `<div class="col-md-3 text-center"><div class="card-label">当前权重</div><div style="font-size:2rem;font-weight:700;color:var(--gold)">${formatNumber(smash.weight, 3)}</div></div><div class="col-md-3 text-center"><div class="card-label">可信度</div><div style="font-size:2rem;font-weight:700;color:${credColor}">${formatNumber(smash.credibility, 3)}</div></div><div class="col-md-3 text-center"><div class="card-label">连续误判</div><div style="font-size:2rem;font-weight:700">${smash.consecutive_misses}</div></div><div class="col-md-3 text-center"><div class="card-label">状态</div><div style="font-size:1.3rem;font-weight:700;color:${credColor}">${smash.credibility >= 0.7 ? '🟢 健康' : smash.credibility >= 0.4 ? '🟡 警告' : '🔴 危险'}</div></div>`;
}
function renderWeightHistory(details) {
    const ctx = document.getElementById('weightHistoryChart');
    if (weightHistInstance) weightHistInstance.destroy();
    const allDates = new Set();
    details.forEach(d => (d.history || []).forEach(h => { if (h.date) allDates.add(h.date); }));
    const dates = [...allDates].sort();
    if (dates.length === 0) { weightHistInstance = new Chart(ctx, { type: 'line', data: { labels: ['暂无历史数据'], datasets: [] }, options: { responsive: true, maintainAspectRatio: false } }); return; }
    const keyFactors = ['smash_factor', 'momentum_factor', 'cycle_factor', 'continuation_factor'];
    const colors = ['#f0b90b', '#1e90ff', '#0ecb81', '#f6465d'];
    const datasets = keyFactors.map((name, idx) => {
        const detail = details.find(d => d.name === name);
        const history = detail ? (detail.history || []) : [];
        const dateWeightMap = {};
        history.forEach(h => { if (h.date) dateWeightMap[h.date] = h.weight; });
        return { label: factorLabels[name] || name, data: dates.map(d => dateWeightMap[d] !== undefined ? dateWeightMap[d] : null), borderColor: colors[idx], backgroundColor: 'transparent', borderWidth: 2, pointRadius: 2, spanGaps: true };
    });
    weightHistInstance = new Chart(ctx, { type: 'line', data: { labels: dates, datasets }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#9ca3af' } } }, scales: { x: { ticks: { color: '#9ca3af', maxRotation: 45, font: { size: 9 } }, grid: { color: 'rgba(45,55,72,0.3)' } }, y: { min: 0, max: 1, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } } });
}
function renderCorrectionLogs(logs) {
    const tbody = document.getElementById('correction-log-body');
    if (!logs || logs.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="text-muted">暂无修正记录</td></tr>'; return; }
    tbody.innerHTML = logs.slice(0, 30).map(log => `<tr><td>${log.date || '--'}</td><td>${log.trigger || '--'}</td><td>${factorLabels[log.factor_name] || log.factor_name}</td><td>${formatNumber(log.old_weight, 3)}</td><td>${formatNumber(log.new_weight, 3)}</td><td>${log.reason || '--'}</td></tr>`).join('');
}

// ============ V4: 智能推荐 ============
let recPollTimer = null, radarChartInstance = null;
const dimLabels = { 'concept_heat': '概念热度', 'board_position': '连板位置', 'seal_quality': '封板质量', 'cap_fit': '市值适配', 'volume_price': '量价配合' };

async function runRecommend() {
    const btn = document.getElementById('btn-run-recommend');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 分析中...';
    document.getElementById('recommend-progress').style.display = 'block';
    document.getElementById('recommend-results').style.display = 'none';
    try {
        const resp = await postJSON('/api/recommend', {});
        if (resp.success) pollRecommendStatus(resp.task_id);
        else { alert('启动失败: ' + (resp.error || '')); resetRecBtn(); }
    } catch (e) { alert('请求失败'); resetRecBtn(); }
}
function pollRecommendStatus(taskId) {
    recPollTimer = setInterval(async () => {
        const resp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
        if (!resp.success) return;
        const d = resp.data;
        document.getElementById('rec-progress-bar').style.width = d.progress + '%';
        document.getElementById('rec-status-text').textContent = d.message;
        if (d.status === 'completed') { 
            clearInterval(recPollTimer); 
            document.getElementById('rec-spinner').style.display = 'none'; 
            resetRecBtn(); 
            if (d.result) showRecommendResults(d.result);
            console.log('推荐任务完成，结果:', d.result); // 添加日志
        }
        else if (d.status === 'error') { 
            clearInterval(recPollTimer); 
            resetRecBtn(); 
        }
    }, 2000);
}
function resetRecBtn() { const btn = document.getElementById('btn-run-recommend'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> 生成智能推荐'; }

function getConfidenceBadge(level) {
    const colors = {
        'S': 'background:linear-gradient(135deg,#f0b90b,#ff6b6b);color:#fff;font-weight:700',
        'A': 'background:rgba(14,203,129,0.2);color:#0ecb81;font-weight:600',
        'B': 'background:rgba(34,211,238,0.15);color:#22d3ee',
        'C': 'background:rgba(156,163,175,0.2);color:#9ca3af',
    };
    const names = {'S':'S级·极高确定性','A':'A级·高确定性','B':'B级·较高','C':'C级·中等'};
    const style = colors[level] || colors['C'];
    return `<span class="rec-stock-tag" style="${style};border:1px solid;border-radius:4px;padding:2px 8px;font-size:0.8rem">${names[level] || level+'级'}</span>`;
}

// ★★★ 修复：showRecommendResults 函数，确保所有字段都有默认值 ★★★
function showRecommendResults(result) {
    document.getElementById('recommend-results').style.display = 'block';
    
    // ★★★ 强制补全推荐数据中的缺失字段 ★★★
    const recs = (result.recommendations || []).map(r => ({
        ...r,
        confidence_level: r.confidence_level || 'C',
        confidence_name: r.confidence_name || 'C级·中等',
        historical_win_rate: r.historical_win_rate || 0.50,
        condition_match: r.condition_match || '',
        risk_notes: Array.isArray(r.risk_notes) ? r.risk_notes : [],
        suggested_action: r.suggested_action || '观望',
        dimension_scores: r.dimension_scores || {
            concept_heat: 0,
            board_position: 0,
            seal_quality: 0,
            cap_fit: 0,
            volume_price: 0
        },
        reason: r.reason || '暂无详细理由'
    }));
    
    // 市场状态
    const ms = result.market_state || {};
    // 出击建议
    const actionAdvice = ms.action_advice || '';
    const actionHtml = actionAdvice ? `
        <div style="grid-column:1/-1;background:rgba(240,185,11,0.08);border:1px solid rgba(240,185,11,0.3);border-radius:8px;padding:10px 14px;margin:4px 0">
            <div style="color:#f0b90b;font-weight:600;font-size:0.85rem"><i class="bi bi-crosshair"></i> 出击建议</div>
            <div style="color:#e5e7eb;font-size:0.8rem;margin-top:4px">${actionAdvice}</div>
        </div>
    ` : '';
    document.getElementById('rec-market-state').innerHTML = `
        ${actionHtml}
        <div class="market-state-item"><span class="market-state-label">周期阶段</span><span class="market-state-value">${ms.cycle_phase || '--'}</span></div>
        <div class="market-state-item"><span class="market-state-label">砸盘系数</span><span class="market-state-value ${getSmashColor(ms.smash_coefficient)}">${formatNumber(ms.smash_coefficient)}</span></div>
        <div class="market-state-item"><span class="market-state-label">炸板率</span><span class="market-state-value">${formatPercent(ms.explosion_rate)}</span></div>
        <div class="market-state-item"><span class="market-state-label">涨停数</span><span class="market-state-value">${ms.limit_up_count || '--'}</span></div>
        <div class="market-state-item"><span class="market-state-label">最高连板</span><span class="market-state-value">${ms.max_boards || '--'}</span></div>
        <div class="market-state-item"><span class="market-state-label">市场情绪</span><span class="market-state-value">${ms.sentiment || '--'}</span></div>
        <div class="market-state-item"><span class="market-state-label">热门概念</span><span class="market-state-value" style="font-size:0.8rem">${(ms.hot_concepts_top5 || []).join(', ') || '--'}</span></div>
    `;
    // 次日策略
    const ns = result.next_day_strategy || {};
    document.getElementById('rec-next-strategy').innerHTML = `
        <div class="strategy-section"><div class="strategy-label"><i class="bi bi-compass"></i> 整体策略</div><div class="strategy-text">${ns.overall_strategy || '--'}</div></div>
        <div class="row g-3">
            <div class="col-md-4"><div class="strategy-section"><div class="strategy-label">目标连板高度</div><div class="strategy-text">${ns.target_board_height || '--'}</div></div></div>
            <div class="col-md-8"><div class="strategy-section"><div class="strategy-label">关注概念</div><div class="strategy-text">${(ns.focus_concepts || []).join(', ') || '--'}</div></div></div>
        </div>
        <div class="strategy-section"><div class="strategy-label"><i class="bi bi-shield-exclamation"></i> 风控要点</div><div class="strategy-text">${ns.risk_control || '--'}</div></div>
    `;
    // 推荐个股（使用补全后的 recs）
    let stockHtml = '';
    if (recs.length === 0) {
        stockHtml = `
            <div style="text-align:center;padding:30px;background:rgba(246,70,93,0.05);border:1px dashed rgba(246,70,93,0.3);border-radius:12px">
                <div style="font-size:2rem;margin-bottom:10px">🛡️</div>
                <div style="color:#f6465d;font-weight:600;font-size:1rem">当前无高确定性标的</div>
                <div style="color:#9ca3af;font-size:0.85rem;margin-top:6px">市场条件不满足信心等级要求，建议空仓观望</div>
                <div style="color:#6b7280;font-size:0.75rem;margin-top:8px">系统只在封单比≥5%+板级≥2时才推荐，宁可错过不可做错</div>
            </div>`;
    } else {
        recs.forEach((r, idx) => {
            // 使用补全后的安全字段
            const confLevel = r.confidence_level;
            const confName = r.confidence_name;
            const histWinRate = r.historical_win_rate;
            const condMatch = r.condition_match;
            const dimScores = r.dimension_scores || {};
            
            // 维度条
            let dimBarsHtml = Object.entries(dimScores).map(([dim, val]) => {
                const color = val >= 70 ? 'var(--green)' : val >= 50 ? 'var(--yellow)' : 'var(--red)';
                return `<div class="dim-score-bar"><span class="dim-score-label">${dimLabels[dim] || dim}</span><div class="dim-score-track"><div class="dim-score-fill" style="width:${val}%;background:${color}"></div></div><span class="dim-score-val">${val}</span></div>`;
            }).join('');
            
            const confBadge = getConfidenceBadge(confLevel);
            const histWinText = histWinRate ? `${(histWinRate * 100).toFixed(0)}%` : '';
            const condText = condMatch || '';
            
            stockHtml += `
                <div class="rec-stock-card" style="border-left:3px solid ${confLevel === 'S' ? '#f0b90b' : confLevel === 'A' ? '#0ecb81' : '#22d3ee'}">
                    <div class="rec-stock-header">
                        <div>
                            <span class="rec-stock-name">${idx + 1}. ${r.name || '--'}</span>
                            <span class="rec-stock-code">${r.code || ''}</span>
                            ${confBadge}
                        </div>
                        <div class="rec-stock-score">${r.total_score || 0}</div>
                    </div>
                    ${condText ? `<div style="font-size:0.75rem;color:#f0b90b;margin:4px 0 6px"><i class="bi bi-patch-check-fill"></i> ${condText} · 历史胜率${histWinText}</div>` : ''}
                    <div class="rec-stock-meta">
                        <span class="rec-stock-tag">${r.concept || '未知概念'}</span>
                        <span class="rec-stock-tag">${r.limit_up_days || 1}连板</span>
                        <span class="rec-stock-tag action">${r.suggested_action || '--'}</span>
                    </div>
                    <div class="row"><div class="col-md-6">${dimBarsHtml}</div><div class="col-md-6"><div class="rec-stock-reason"><i class="bi bi-lightbulb"></i> ${r.reason || '--'}</div></div></div>
                    ${r.risk_notes && r.risk_notes.length > 0 ? `<div class="rec-stock-risks"><i class="bi bi-exclamation-triangle"></i> ${r.risk_notes.join('；')}</div>` : ''}
                </div>`;
        });
    }
    document.getElementById('rec-stock-list').innerHTML = stockHtml;
    // 雷达图（使用补全后的 recs）
    renderRadarChart(recs);
    // 出场信号面板
    loadExitSignals();
}

function renderRadarChart(recs) {
    const ctx = document.getElementById('radarChart');
    if (radarChartInstance) radarChartInstance.destroy();
    // ★★★ 空数组保护 ★★★
    if (!recs || recs.length === 0) {
        console.warn('雷达图无数据，跳过渲染');
        return;
    }
    const dims = ['concept_heat', 'board_position', 'seal_quality', 'cap_fit', 'volume_price'];
    const colors = ['#f0b90b', '#22d3ee', '#0ecb81', '#a855f7', '#f6465d', '#1e90ff', '#fcd535', '#ff6b6b', '#4ecdc4', '#45b7d1'];
    const datasets = recs.slice(0, 5).map((r, idx) => {
        const ds = r.dimension_scores || {};
        return {
            label: r.name || `股票${idx+1}`,
            data: dims.map(d => ds[d] || 0),
            borderColor: colors[idx % colors.length],
            backgroundColor: colors[idx % colors.length] + '20',
            borderWidth: 2,
            pointRadius: 3,
        };
    });
    radarChartInstance = new Chart(ctx, {
        type: 'radar',
        data: { labels: dims.map(d => dimLabels[d] || d), datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#9ca3af' } } },
            scales: { r: { min: 0, max: 100, ticks: { color: '#9ca3af', backdropColor: 'transparent' }, grid: { color: 'rgba(45,55,72,0.5)' }, pointLabels: { color: '#e5e7eb', font: { size: 12 } }, angleLines: { color: 'rgba(45,55,72,0.5)' } } }
        }
    });
}

// ============ 出场信号面板 ============
async function loadExitSignals() {
    try {
        const resp = await fetchJSON('/api/exit-signals');
        if (!resp.success) return;
        const data = resp.data;
        renderExitSignals(data);
    } catch (e) {
        console.warn('出场信号加载失败:', e);
    }
}

function renderExitSignals(data) {
    const container = document.getElementById('exit-signals-panel');
    const loading = document.getElementById('exit-signals-loading');
    if (!container) return;
    if (loading) loading.style.display = 'none';
    container.style.display = 'block';
    
    const market = data.market_advice || {};
    const stocks = data.stock_advices || [];
    const action = data.overall_action || 'NORMAL';
    
    // 综合行动横幅
    const actionConfig = {
        'CLEAR_ALL': { icon: '🔴', text: '清仓观望', bg: 'rgba(239,83,80,0.12)', border: 'rgba(239,83,80,0.4)', color: '#ef5350' },
        'REDUCE':    { icon: '🟠', text: '减仓防守', bg: 'rgba(255,152,0,0.12)', border: 'rgba(255,152,0,0.4)', color: '#ff9800' },
        'HOLD':      { icon: '🟡', text: '保持仓位，不出新仓', bg: 'rgba(255,235,59,0.12)', border: 'rgba(255,235,59,0.4)', color: '#ffeb3b' },
        'NORMAL':    { icon: '🟢', text: '正常参与', bg: 'rgba(14,203,129,0.12)', border: 'rgba(14,203,129,0.4)', color: '#0ecb81' }
    };
    const ac = actionConfig[action] || actionConfig['NORMAL'];
    
    // 市场信号
    const marketSignals = market.market_signals || [];
    let marketHtml = '';
    if (marketSignals.length > 0) {
        marketHtml = marketSignals.map(sig => {
            const sevColor = sig.severity === 'CRITICAL' ? '#ef5350' : sig.severity === 'HIGH' ? '#ff9800' : '#ffeb3b';
            const sevIcon = sig.severity === 'CRITICAL' ? 'bi-exclamation-octagon-fill' : sig.severity === 'HIGH' ? 'bi-exclamation-triangle-fill' : 'bi-info-circle-fill';
            return `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 10px;background:rgba(0,0,0,0.15);border-radius:6px;border-left:3px solid ${sevColor};margin-bottom:6px">
                <i class="bi ${sevIcon}" style="color:${sevColor};font-size:1rem;margin-top:2px"></i>
                <div style="flex:1"><div style="color:${sevColor};font-weight:600;font-size:0.8rem">${sig.signal_name}</div>
                <div style="color:#e5e7eb;font-size:0.75rem;margin-top:2px">${sig.description}</div>
                <div style="color:#9ca3af;font-size:0.7rem;margin-top:4px">→ ${sig.action}</div></div>
            </div>`;
        }).join('');
    } else {
        marketHtml = '<div style="color:#0ecb81;font-size:0.8rem;padding:8px"><i class="bi bi-check-circle-fill"></i> 市场无明显风险信号</div>';
    }
    
    // 个股出场信号（只显示有信号的）
    let stockHtml = '';
    const riskyStocks = stocks.filter(s => s.exit_signals && s.exit_signals.length > 0);
    if (riskyStocks.length > 0) {
        stockHtml = riskyStocks.map(s => {
            const urgColor = s.exit_urgency === 'CRITICAL' ? '#ef5350' : s.exit_urgency === 'HIGH' ? '#ff9800' : '#ffeb3b';
            const signalsHtml = s.exit_signals.map(sig => {
                const sevColor = sig.severity === 'CRITICAL' ? '#ef5350' : sig.severity === 'HIGH' ? '#ff9800' : '#ffeb3b';
                return `<div style="font-size:0.75rem;color:${sevColor};margin-top:4px">⚠ ${sig.signal_name}: ${sig.description}<br><span style="color:#9ca3af">→ ${sig.action}</span></div>`;
            }).join('');
            return `<div style="padding:8px 10px;background:rgba(0,0,0,0.15);border-radius:6px;border-left:3px solid ${urgColor};margin-bottom:6px">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="color:#e5e7eb;font-weight:600;font-size:0.85rem">${s.stock_name}(${s.stock_code})</span>
                    <span style="color:${urgColor};font-size:0.7rem;font-weight:600">${s.current_boards}板 · ${s.exit_urgency}</span>
                </div>
                ${signalsHtml}
            </div>`;
        }).join('');
    } else {
        stockHtml = '<div style="color:#0ecb81;font-size:0.8rem;padding:8px"><i class="bi bi-check-circle-fill"></i> 高板个股暂无出场信号</div>';
    }
    
    container.innerHTML = `
        <div style="background:${ac.bg};border:1px solid ${ac.border};border-radius:8px;padding:10px 14px;margin-bottom:10px">
            <div style="color:${ac.color};font-weight:700;font-size:0.9rem">${ac.icon} 出场建议: ${ac.text}</div>
            <div style="color:#9ca3af;font-size:0.75rem;margin-top:4px">${market.position_suggestion || ''}</div>
        </div>
        <div class="row g-3">
            <div class="col-md-6">
                <div style="font-size:0.8rem;color:#9ca3af;margin-bottom:6px"><i class="bi bi-bar-chart-fill"></i> 市场风险</div>
                ${marketHtml}
            </div>
            <div class="col-md-6">
                <div style="font-size:0.8rem;color:#9ca3af;margin-bottom:6px"><i class="bi bi-graph-up"></i> 个股风险</div>
                ${stockHtml}
            </div>
        </div>
    `;
}

async function loadRecommendationHistory() {
    try {
        const resp = await fetchJSON('/api/recommendations/history?limit=50');
        if (!resp.success) return;
        const data = resp.data;
        const tbody = document.getElementById('rec-history-body');
        if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan="7" class="text-muted">暂无推荐历史</td></tr>'; return; }
        tbody.innerHTML = data.map(r => {
            const mark = r.is_correct === 1 ? '✅' : r.is_correct === 0 ? '❌' : '⏳';
            return `<tr><td>${r.rec_date || '--'}</td><td>${r.code || ''}</td><td>${r.name || ''}</td><td>${formatNumber(r.score, 1)}</td><td>${formatPercent(r.win_rate_estimate)}</td><td>${r.suggested_action || '--'}</td><td>${mark} ${r.actual_result || ''}</td></tr>`;
        }).join('');
    } catch (e) { console.error(e); }
}

// ============ V4: 实盘跟踪 ============
let trackPollTimer = null;

async function runTrack() {
    const btn = document.getElementById('btn-run-track');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 跟踪中...';
    document.getElementById('track-progress').style.display = 'block';
    document.getElementById('track-results').style.display = 'none';
    try {
        const resp = await postJSON('/api/track', {});
        if (resp.success) pollTrackStatus(resp.task_id);
        else { alert('启动失败'); resetTrackBtn(); }
    } catch (e) { alert('请求失败'); resetTrackBtn(); }
}
function pollTrackStatus(taskId) {
    trackPollTimer = setInterval(async () => {
        const resp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
        if (!resp.success) return;
        const d = resp.data;
        document.getElementById('track-progress-bar').style.width = d.progress + '%';
        document.getElementById('track-status-text').textContent = d.message;
        if (d.status === 'completed') { clearInterval(trackPollTimer); document.getElementById('track-spinner').style.display = 'none'; resetTrackBtn(); if (d.result) showTrackResults(d.result); }
        else if (d.status === 'error') { clearInterval(trackPollTimer); resetTrackBtn(); }
    }, 2000);
}
function resetTrackBtn() { const btn = document.getElementById('btn-run-track'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> 执行实盘跟踪'; }

function showTrackResults(result) {
    document.getElementById('track-results').style.display = 'block';
    const tracking = result.tracking || {};
    const signals = result.signals || {};
    // 胜率仪表盘
    const cumWR = tracking.cumulative_win_rate || 0;
    document.getElementById('track-winrate').textContent = formatPercent(cumWR);
    document.getElementById('track-total-rec').textContent = result.cumulative?.total_recommendations || tracking.recommendations_count || '--';
    document.getElementById('track-total-correct').textContent = result.cumulative?.total_correct || tracking.correct_count || '--';
    document.getElementById('track-daily-winrate').textContent = formatPercent(tracking.win_rate);
    // 对比表
    const details = tracking.details || [];
    const tbody = document.getElementById('track-detail-body');
    if (details.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="text-muted">当日无推荐记录</td></tr>'; }
    else {
        tbody.innerHTML = details.map(d => {
            const mark = d.is_correct ? '✅ 命中' : '❌ 未命中';
            return `<tr><td>${d.code}</td><td>${d.name}</td><td>${formatNumber(d.score, 1)}</td><td>${d.suggested_action || '--'}</td><td>${d.result_desc || '--'}</td><td>${mark}</td></tr>`;
        }).join('');
    }
    // 信号面板
    const signalDefs = {1:'龙头断板反转',2:'砸盘系数骤降',3:'概念集中度爆发',4:'炸板率飙升',5:'连板梯队断层',6:'情绪冰点反转',7:'龙头加速',8:'高低切换'};
    const triggered = (signals.triggered || []).map(s => s.signal_id);
    const panel = document.getElementById('track-signal-panel');
    panel.innerHTML = Object.entries(signalDefs).map(([id, name]) => {
        const isActive = triggered.includes(parseInt(id));
        return `<div class="col-6 col-md-3"><div class="signal-card ${isActive ? 'triggered' : ''}"><div class="signal-header"><span class="signal-name">${name}</span><span class="signal-indicator ${isActive ? 'active' : 'inactive'}"></span></div><div class="signal-desc">信号${id}</div></div></div>`;
    }).join('');
}

// ============ V4: 自适应升级 ============
let upgradePollTimer = null, weightCompareInstance = null, weightAdjHistInstance = null;

async function runAutoUpgrade(checkOnly) {
    const btn = document.getElementById('btn-run-upgrade');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 升级中...';
    document.getElementById('upgrade-progress').style.display = 'block';
    document.getElementById('upgrade-results').style.display = 'none';
    try {
        const resp = await postJSON('/api/auto-upgrade', { check_only: checkOnly });
        if (resp.success) pollUpgradeStatus(resp.task_id);
        else { alert('启动失败'); resetUpgradeBtn(); }
    } catch (e) { alert('请求失败'); resetUpgradeBtn(); }
}
function pollUpgradeStatus(taskId) {
    upgradePollTimer = setInterval(async () => {
        const resp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
        if (!resp.success) return;
        const d = resp.data;
        document.getElementById('upgrade-progress-bar').style.width = d.progress + '%';
        document.getElementById('upgrade-status-text').textContent = d.message;
        if (d.status === 'completed') { clearInterval(upgradePollTimer); document.getElementById('upgrade-spinner').style.display = 'none'; resetUpgradeBtn(); if (d.result) showUpgradeResults(d.result); }
        else if (d.status === 'error') { clearInterval(upgradePollTimer); resetUpgradeBtn(); }
    }, 2000);
}
function resetUpgradeBtn() { const btn = document.getElementById('btn-run-upgrade'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-play-fill"></i> 执行自动升级'; }

function showUpgradeResults(result) {
    document.getElementById('upgrade-results').style.display = 'block';
    const accuracy = result.accuracy || {};
    const weightAdj = result.weight_adjust || {};
    const regime = result.regime || {};
    // 准确性概览
    document.getElementById('upg-total-rec').textContent = accuracy.total_recommendations || '--';
    document.getElementById('upg-winrate').textContent = formatPercent(accuracy.overall_win_rate);
    const period = accuracy.period || [];
    document.getElementById('upg-period').textContent = period.length >= 2 ? `${period[0]} ~ ${period[1]}` : '--';
    // 权重可视化
    const oldW = weightAdj.old_weights || {};
    const newW = weightAdj.new_weights || {};
    let weightsHtml = '';
    Object.keys(newW).forEach(dim => {
        const oldVal = oldW[dim] || 0;
        const newVal = newW[dim] || 0;
        const changed = Math.abs(oldVal - newVal) > 0.001;
        const arrow = changed ? (newVal > oldVal ? '↑' : '↓') : '';
        weightsHtml += `<div class="weight-slider-group"><div class="weight-slider-label"><span class="weight-slider-name">${dimLabels[dim] || dim}</span><span class="weight-slider-val">${newVal.toFixed(3)} ${arrow}</span></div><div class="dim-score-track"><div class="dim-score-fill" style="width:${newVal*100}%;background:${changed ? 'var(--cyan)' : 'var(--gold)'}"></div></div></div>`;
    });
    document.getElementById('upg-weights-display').innerHTML = weightsHtml;
    // 权重对比图
    renderWeightCompare(oldW, newW);
    // 风格检测
    const regimeHtml = `
        <div class="row g-3">
            <div class="col-md-4 text-center"><div class="card-label">当前风格</div><div class="regime-badge current" style="margin-top:0.5rem">${regime.current_regime || '--'}</div></div>
            <div class="col-md-4 text-center"><div class="card-label">上一风格</div><div style="margin-top:0.5rem;font-size:1rem;color:var(--text-muted)">${regime.prev_regime || '--'}</div></div>
            <div class="col-md-4 text-center"><div class="card-label">状态</div><div style="margin-top:0.5rem;font-size:1.2rem">${regime.is_changed ? '<span class="regime-badge changed">⚠️ 风格切换</span>' : '<span style="color:var(--green)">✅ 稳定</span>'}</div></div>
        </div>
        ${(regime.evidence || []).length > 0 ? `<div style="margin-top:1rem"><div class="strategy-label">检测依据</div>${regime.evidence.map(e => `<div class="strategy-text">• ${e}</div>`).join('')}</div>` : ''}
    `;
    document.getElementById('upg-regime-display').innerHTML = regimeHtml;
    // 加载升级日志
    loadUpgradeLogs();
}

function renderWeightCompare(oldW, newW) {
    const ctx = document.getElementById('weightCompareChart');
    if (weightCompareInstance) weightCompareInstance.destroy();
    const dims = Object.keys(newW);
    const labels = dims.map(d => dimLabels[d] || d);
    weightCompareInstance = new Chart(ctx, {
        type: 'bar', data: { labels, datasets: [
            { label: '调整前', data: dims.map(d => oldW[d] || 0), backgroundColor: 'rgba(100,116,139,0.6)', borderRadius: 4 },
            { label: '调整后', data: dims.map(d => newW[d] || 0), backgroundColor: 'rgba(34,211,238,0.6)', borderColor: 'var(--cyan)', borderWidth: 1, borderRadius: 4 }
        ] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#9ca3af' } } }, scales: { x: { ticks: { color: '#9ca3af', font: { size: 10 } }, grid: { display: false } }, y: { min: 0, max: 0.5, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } }
    });
}

async function loadWeightHistory() {
    try {
        const resp = await fetchJSON('/api/weights/history');
        if (!resp.success) return;
        const data = resp.data;
        renderWeightAdjustHistory(data);
    } catch (e) { console.error(e); }
}

function renderWeightAdjustHistory(history) {
    const ctx = document.getElementById('weightAdjustHistChart');
    if (weightAdjHistInstance) weightAdjHistInstance.destroy();
    if (!history || history.length === 0) {
        weightAdjHistInstance = new Chart(ctx, { type: 'line', data: { labels: ['暂无历史'], datasets: [] }, options: { responsive: true, maintainAspectRatio: false } });
        return;
    }
    // 按日期分组
    const dateMap = {};
    history.reverse().forEach(h => {
        const d = h.adjust_date;
        if (!dateMap[d]) dateMap[d] = {};
        dateMap[d][h.dimension] = h.new_weight;
    });
    const dates = Object.keys(dateMap).sort();
    const dims = ['concept_heat', 'board_position', 'seal_quality', 'cap_fit', 'volume_price'];
    const colors = ['#f0b90b', '#22d3ee', '#0ecb81', '#a855f7', '#f6465d'];
    const datasets = dims.map((dim, idx) => ({
        label: dimLabels[dim] || dim,
        data: dates.map(d => dateMap[d]?.[dim] ?? null),
        borderColor: colors[idx],
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 3,
        spanGaps: true,
    }));
    weightAdjHistInstance = new Chart(ctx, {
        type: 'line', data: { labels: dates, datasets },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#9ca3af' } } }, scales: { x: { ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: 'rgba(45,55,72,0.3)' } }, y: { min: 0, max: 0.5, ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } } } }
    });
}

async function loadUpgradeLogs() {
    try {
        const resp = await fetchJSON('/api/upgrade/logs');
        if (!resp.success) return;
        const data = resp.data;
        const tbody = document.getElementById('upgrade-log-body');
        if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan="4" class="text-muted">暂无升级日志</td></tr>'; return; }
        tbody.innerHTML = data.map(log => {
            let detail = log.details || '';
            try { detail = JSON.stringify(JSON.parse(detail)).substring(0, 100) + '...'; } catch(e) {}
            return `<tr><td>${log.upgrade_date || '--'}</td><td>${log.upgrade_type || '--'}</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${detail}</td><td>${log.status || '--'}</td></tr>`;
        }).join('');
    } catch (e) { console.error(e); }
}

// ============ V4: 信号监控 ============
async function loadSignalsTab() {
    await loadSignalCards();
    await loadSignalStats();
    await loadSignalTimeline();
}

async function loadSignalCards() {
    try {
        const resp = await fetchJSON('/api/signals');
        if (!resp.success) return;
        const signals = resp.data;
        const container = document.getElementById('signal-cards-container');
        if (!signals || signals.length === 0) { container.innerHTML = '<div class="text-muted">暂无信号数据</div>'; return; }
        container.innerHTML = '<div class="row g-3">' + signals.map(s => {
            const wrColor = s.win_rate >= 70 ? 'var(--green)' : s.win_rate >= 50 ? 'var(--yellow)' : 'var(--red)';
            return `<div class="col-md-4 col-lg-3"><div class="signal-card"><div class="signal-header"><span class="signal-name">${s.name}</span><span class="signal-indicator ${s.trigger_count > 0 ? 'active' : 'inactive'}"></span></div><div class="signal-desc">${s.conditions}</div><div class="signal-stats"><span class="signal-stat-item">触发 <span class="signal-stat-val">${s.trigger_count}</span>次</span><span class="signal-stat-item">胜率 <span class="signal-stat-val" style="color:${wrColor}">${formatNumber(s.win_rate, 1)}%</span></span><span class="signal-stat-item">均收益 <span class="signal-stat-val">${formatNumber(s.avg_return, 1)}%</span></span></div></div></div>`;
        }).join('') + '</div>';
    } catch (e) { console.error(e); }
}

async function loadSignalStats() {
    try {
        const resp = await fetchJSON('/api/signals');
        if (!resp.success) return;
        const signals = resp.data;
        const tbody = document.getElementById('signal-stats-body');
        if (!signals || signals.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="text-muted">暂无数据</td></tr>'; return; }
        tbody.innerHTML = signals.map(s => `<tr><td>${s.signal_id}</td><td>${s.name}</td><td>${s.trigger_count}</td><td style="color:${s.win_rate >= 70 ? 'var(--green)' : s.win_rate >= 50 ? 'var(--yellow)' : 'var(--red)'}">${formatNumber(s.win_rate, 1)}%</td><td>${formatNumber(s.avg_return, 1)}%</td><td style="max-width:200px;font-size:0.75rem">${s.conditions}</td></tr>`).join('');
    } catch (e) { console.error(e); }
}

async function loadSignalTimeline() {
    try {
        const resp = await fetchJSON('/api/signals/history');
        if (!resp.success) return;
        const data = resp.data;
        const container = document.getElementById('signal-timeline');
        if (!data || data.length === 0) { container.innerHTML = '<div class="text-muted">暂无触发记录</div>'; return; }
        const signalNames = {1:'龙头断板反转',2:'砸盘系数骤降',3:'概念集中度爆发',4:'炸板率飙升',5:'连板梯队断层',6:'情绪冰点反转',7:'龙头加速',8:'高低切换'};
        container.innerHTML = data.slice(0, 30).map(item => {
            const name = signalNames[item.signal_id] || `信号${item.signal_id}`;
            let stocks = '';
            try { stocks = JSON.parse(item.trigger_stocks || '[]').join(', '); } catch(e) { stocks = item.trigger_stocks || ''; }
            return `<div class="timeline-item"><span class="timeline-date">${item.trigger_date}</span><span class="timeline-content"><strong>${name}</strong> ${stocks}${item.avg_return != null ? ` → 收益${formatNumber(item.avg_return, 1)}%` : ''}</span></div>`;
        }).join('');
    } catch (e) { console.error(e); }
}

// ============ 模拟交易 ============
let simPollTimer = null;
let simNetChartInstance = null;

async function runSimulate() {
    const btn = document.getElementById('btn-run-simulate');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 启动中...';
    document.getElementById('simulate-progress').style.display = 'block';
    document.getElementById('simulate-results').style.display = 'none';

    // 收集参数
    const startDate = document.getElementById('sim-start-date').value;
    const endDate = document.getElementById('sim-end-date').value;
    const initCash = parseFloat(document.getElementById('sim-init-cash').value) * 10000;
    const gradeFilter = JSON.parse(document.getElementById('sim-grade-filter').value);
    const takeProfit = parseFloat(document.getElementById('sim-take-profit').value) / 100;
    const stopLoss = parseFloat(document.getElementById('sim-stop-loss').value) / 100;
    const maxPositions = parseInt(document.getElementById('sim-max-positions').value);
    const positionPct = parseFloat(document.getElementById('sim-position-pct').value) / 100;

    try {
        const resp = await postJSON('/api/simulate', {
            start_date: startDate,
            end_date: endDate,
            init_cash: initCash,
            grade_filter: gradeFilter,
            take_profit: takeProfit,
            stop_loss: stopLoss,
            max_positions: maxPositions,
            position_pct: positionPct
        });
        if (!resp.success) {
            alert('启动失败: ' + (resp.error || ''));
            resetSimBtn();
            return;
        }
        const taskId = resp.task_id;
        simPollTimer = setInterval(async () => {
            try {
                const statusResp = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
                if (!statusResp.success) return;
                const d = statusResp.data;
                document.getElementById('sim-progress-bar').style.width = d.progress + '%';
                document.getElementById('sim-status-text').textContent = d.message;
                if (d.status === 'completed') {
                    clearInterval(simPollTimer);
                    document.getElementById('sim-spinner').style.display = 'none';
                    resetSimBtn();
                    if (d.result) showSimulateResults(d.result);
                } else if (d.status === 'error') {
                    clearInterval(simPollTimer);
                    resetSimBtn();
                    alert('回测出错: ' + d.message);
                }
            } catch (e) {
                console.error(e);
            }
        }, 2000);
    } catch (e) {
        alert('请求失败: ' + e.message);
        resetSimBtn();
    }
}

function resetSimBtn() {
    const btn = document.getElementById('btn-run-simulate');
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-play-fill"></i> 开始回测';
}

function showSimulateResults(result) {
    document.getElementById('simulate-results').style.display = 'block';
    // 统计指标
    const totalReturn = (result.total_return * 100).toFixed(2);
    const annualReturn = (result.annual_return * 100).toFixed(2);
    const maxDrawdown = (result.max_drawdown * 100).toFixed(2);
    const winRate = (result.win_rate * 100).toFixed(1);
    const profitLossRatio = result.profit_loss_ratio.toFixed(2);
    const sharpe = result.sharpe_ratio.toFixed(2);
    const totalTrades = result.total_trades;

    let html = `
        <div class="row g-2">
            <div class="col-6 col-md-3"><div class="card-label">累计收益率</div><div class="card-value" style="color:${totalReturn >= 0 ? 'var(--green)' : 'var(--red)'}">${totalReturn}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">年化收益率</div><div class="card-value" style="color:${annualReturn >= 0 ? 'var(--green)' : 'var(--red)'}">${annualReturn}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">最大回撤</div><div class="card-value" style="color:var(--red)">${maxDrawdown}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">胜率</div><div class="card-value">${winRate}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">盈亏比</div><div class="card-value">${profitLossRatio}</div></div>
            <div class="col-6 col-md-3"><div class="card-label">夏普比率</div><div class="card-value">${sharpe}</div></div>
            <div class="col-6 col-md-3"><div class="card-label">交易次数</div><div class="card-value">${totalTrades}</div></div>
            <div class="col-6 col-md-3"><div class="card-label">期末净值</div><div class="card-value">${result.final_value.toFixed(2)}</div></div>
        </div>
    `;
    document.getElementById('simulate-results').innerHTML = html;

    // 净值曲线
    renderNetChart(result.net_values);

    // 交易明细
    const trades = result.trades || [];
    const tbody = document.getElementById('sim-trade-body');
    if (trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-muted">无交易记录</td></tr>';
    } else {
        tbody.innerHTML = trades.map(t => {
            const profit = t.profit || 0;
            const profitStr = profit !== undefined ? (profit > 0 ? '+' : '') + profit.toFixed(2) : '';
            const color = profit > 0 ? 'var(--green)' : (profit < 0 ? 'var(--red)' : '');
            return `<tr>
                <td>${t.date}</td>
                <td>${t.code}</td>
                <td>${t.action}</td>
                <td>${t.price.toFixed(2)}</td>
                <td>${t.shares}</td>
                <td style="color:${color}">${profitStr}</td>
                <td>${t.reason || ''}</td>
            </tr>`;
        }).join('');
    }
}

function renderNetChart(netValues) {
    const ctx = document.getElementById('simNetChart');
    if (simNetChartInstance) simNetChartInstance.destroy();
    const labels = netValues.map(d => d.date);
    const data = netValues.map(d => d.value);
    simNetChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: '资产净值',
                data: data,
                borderColor: '#f0b90b',
                backgroundColor: 'rgba(240,185,11,0.1)',
                tension: 0.3,
                fill: true,
                pointRadius: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: '#9ca3af' } }
            },
            scales: {
                x: { ticks: { color: '#9ca3af', maxRotation: 45 }, grid: { color: 'rgba(45,55,72,0.3)' } },
                y: { ticks: { color: '#9ca3af' }, grid: { color: 'rgba(45,55,72,0.3)' } }
            }
        }
    });
}

// ============ 初始化 ============
document.addEventListener('DOMContentLoaded', () => {
    loadDashboard();
    document.getElementById('btn-run-simulate').addEventListener('click', runSimulate);
});