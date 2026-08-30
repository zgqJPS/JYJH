// Market Advisor V4 - 前端交互逻辑（修复版）
// 修复：推荐列表 confidence_level/confidence_name/condition_match 为空导致页面空白
// 额外保护：renderRadarChart 空数组保护
// 新增：模拟交易功能
// 新增：量化策略功能
// 新增：资金流、龙头识别、操作计划报告显示
// ★ 优化：操作计划整合到推荐个股下方

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
    if (tab === 'daily') loadDailyLatest();
    if (tab === 'recommend') loadRecommendLatest();
    if (tab === 'reports') loadReports();
    if (tab === 'health') loadModelHealth();
    if (tab === 'signals') loadSignalsTab();
    if (tab === 'simulate') {
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
let turningPointsData = null;

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
        turningPointsData = data.turning_points || null;
        renderSmashChart(data.smash_chart || [], turningPointsData);
        renderTurningPointPanel(turningPointsData);
        renderVpGate(data.volume_price_market);
        renderPredictions(data.predictions || [], 'prediction-cards');
        loadEntryCertainty();
    } catch (e) { console.error('Dashboard load error:', e); }
}

// ─────────── 量价闸门：整体量价走势（筛选与进场首要依据） ───────────
function renderVpGate(vp) {
    const card = document.getElementById('vp-gate-card');
    if (!card) return;
    if (!vp) {
        card.innerHTML = '<h6 class="text-gold"><i class="bi bi-shield-check"></i> 量价闸门 · 整体量价走势（筛选与进场首要依据）</h6><div class="text-muted small">暂无量价数据</div>';
        return;
    }
    const gateMap = {
        '正常参与': { icon: '🟢', bg: 'rgba(14,203,129,0.10)', border: 'rgba(14,203,129,0.45)', color: '#0ecb81' },
        '收缩仓位': { icon: '🟡', bg: 'rgba(250,173,20,0.10)', border: 'rgba(250,173,20,0.45)', color: '#faad14' },
        '全场无买点': { icon: '🔴', bg: 'rgba(246,70,93,0.10)', border: 'rgba(246,70,93,0.50)', color: '#f6465d' },
    };
    const gc = gateMap[vp.gate] || gateMap['收缩仓位'];
    const m = vp.metrics || {};
    const metricItems = [
        ['涨停', m.limit_up_count != null ? m.limit_up_count + '家' : '--'],
        ['跌停', m.limit_down_count != null ? m.limit_down_count + '家' : '--'],
        ['炸板率', m.explosion_rate != null ? (m.explosion_rate * 100).toFixed(0) + '%' : '--'],
        ['平均量比', m.avg_volume_bias != null ? Number(m.avg_volume_bias).toFixed(2) : '--'],
        ['最高板', m.max_boards != null ? m.max_boards + '板' : '--'],
    ];
    const metricHtml = metricItems.map(([k, v]) =>
        `<span style="display:inline-block;margin:2px 10px 2px 0;font-size:0.78rem"><span class="text-muted">${k} </span><b style="color:#e5e7eb">${v}</b></span>`
    ).join('');
    const signals = (vp.signals || []).slice(0, 3).map(s => `<div style="font-size:0.75rem;color:${gc.color};margin-top:2px">✅ ${s}</div>`).join('');
    const risks = (vp.risks || []).slice(0, 3).map(r => `<div style="font-size:0.75rem;color:#f6465d;margin-top:2px">⚠️ ${r}</div>`).join('');
    const note = vp.gate === '全场无买点'
        ? '⛔ 量价闸门关闭：除分歧转一致回封标的外，全场不给买点、不出新仓'
        : vp.gate === '收缩仓位'
            ? '⚠️ 量价环境偏弱：个股量价不合格（fail）一票否决，谨慎仓位减半'
            : '✅ 量价环境支持参与：个股仍需通过量价形态闸门（fail一票否决）';
    card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
            <h6 class="text-gold mb-0"><i class="bi bi-shield-check"></i> 量价闸门 · 整体量价走势（筛选与进场首要依据）</h6>
            <span style="padding:4px 16px;border-radius:20px;background:${gc.bg};border:1px solid ${gc.border};color:${gc.color};font-weight:700;font-size:0.9rem">${gc.icon} ${vp.gate || '--'}</span>
        </div>
        <div style="margin-top:8px;font-size:0.82rem;color:#e5e7eb">
            <b style="color:${gc.color}">${vp.state_label || ''}</b>
            <span class="text-muted ms-2">量价评分 ${vp.score != null ? Math.round(vp.score) : '--'}</span>
        </div>
        <div style="margin-top:6px">${metricHtml}</div>
        ${signals || risks ? `<div style="margin-top:6px">${signals}${risks}</div>` : ''}
        <div style="margin-top:8px;padding:6px 10px;background:${gc.bg};border-radius:6px;font-size:0.75rem;color:${gc.color}">${note}</div>
    `;
}

// ─────────── 一键更新：触发后端全量分析（获取数据+全模块重算），完成后刷新所有页面 ───────────
async function runRefreshAll() {
    const btn = document.getElementById('btn-refresh-all');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const oldHtml = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 更新中...';
    try {
        const resp = await postJSON('/api/daily', {});
        if (!resp.success) { alert('一键更新启动失败：' + (resp.error || '')); btn.disabled = false; btn.innerHTML = oldHtml; return; }
        const taskId = resp.task_id;
        const timer = setInterval(async () => {
            try {
                const sr = await fetchJSON(`/api/daily/status?task_id=${taskId}`);
                if (!sr.success) return;
                const d = sr.data;
                btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${d.progress || 0}% ${d.message ? d.message.substring(0, 12) : ''}`;
                if (d.status === 'completed') {
                    clearInterval(timer);
                    btn.disabled = false;
                    btn.innerHTML = '<i class="bi bi-check-circle-fill"></i> 更新完成';
                    // 全量刷新各页面数据（无论当前在哪个Tab，全部重载）
                    try { await loadDashboard(); } catch (e) {}
                    try { await loadDailyLatest(); } catch (e) {}
                    try { await loadRecommendLatest(); } catch (e) {}
                    try { await loadRecommendationHistory(); } catch (e) {}
                    try { await loadExitSignals(); } catch (e) {}
                    try { await loadReports(); } catch (e) {}
                    try { await loadModelHealth(); } catch (e) {}
                    try { await loadSignalCards(); await loadSignalStats(); } catch (e) {}
                    setTimeout(() => { btn.innerHTML = oldHtml; }, 3000);
                } else if (d.status === 'error') {
                    clearInterval(timer);
                    btn.disabled = false; btn.innerHTML = oldHtml;
                    alert('更新失败：' + (d.message || '请查看日志'));
                }
            } catch (e) { console.warn('refresh poll error:', e); }
        }, 2500);
    } catch (e) {
        alert('一键更新请求失败：' + e.message);
        btn.disabled = false; btn.innerHTML = oldHtml;
    }
}

// ─────────── 进场确定性深度分析 ───────────
async function loadEntryCertainty() {
    try {
        const resp = await fetch('/api/entry_certainty?top_n=12');
        const json = await resp.json();
        if (!json.success || !json.data) return;
        renderEntryCertaintyPanel(json.data);
    } catch (e) { console.error('Entry certainty load error:', e); }
}

function renderEntryCertaintyPanel(data) {
    const panel = document.getElementById('entry-certainty-panel');
    const dateEl = document.getElementById('entry-certainty-date');
    if (!panel) return;
    if (!data.results || data.results.length === 0) {
        // 区分"无分析数据"与"有分析但全部被质量门槛过滤"
        if (data.total_analyzed && data.total_analyzed > 0) {
            panel.innerHTML = `<div style="padding:14px;text-align:center;">
                <div style="font-size:1.6rem">🛡️</div>
                <b style="color:#faad14;">当日 ${data.total_analyzed} 只候选全部未达进场门槛</b>
                <div class="text-muted small mt-1">${data.gate_rule || '综合分/题材/量价不达标'}，宁缺毋滥，建议空仓观望</div>
            </div>`;
        } else {
            panel.innerHTML = '<div class="text-muted">暂无进场确定性分析数据（执行每日分析后生成）</div>';
        }
        if (dateEl) dateEl.textContent = data.date || '';
        return;
    }
    if (dateEl) dateEl.textContent = data.date || '';

    const gradeColor = {
        'S+': 'linear-gradient(135deg,#ff4d4f,#faad14)',
        'S':  'linear-gradient(135deg,#faad14,#ffd666)',
        'A':  'linear-gradient(135deg,#1890ff,#69c0ff)',
        'B':  'linear-gradient(135deg,#52c41a,#95de64)',
        'C':  'linear-gradient(135deg,#8c8c8c,#bfbfbf)',
        'D':  'linear-gradient(135deg,#595959,#8c8c8c)'
    };
    const actionIcon = {
        'board_hit': '🎯', 'half_way': '⚡', 'low_buy': '👋',
        'wait': '🚫', '空仓观望': '🚫', '观望': '👀'
    };

    const esc = s => String(s == null ? '' : s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    const scoreColor = v => v >= 80 ? '#52c41a' : v >= 60 ? '#1890ff' : v >= 40 ? '#faad14' : '#f6465d';
    const gradeIcon = { pass: '🟢', caution: '🟡', fail: '🔴' };

    // ── 单维度深度块（题材/卡位/换手/竞价/次日）──
    function dimBlock(icon, title, d) {
        if (!d) return '';
        const sig = (d.signals || []).slice(0, 3).map(s => `<div style="color:#73d13d;font-size:11px;line-height:1.5">✅ ${esc(s)}</div>`).join('');
        const rsk = (d.risks || []).slice(0, 3).map(s => `<div style="color:#ff7875;font-size:11px;line-height:1.5">⚠️ ${esc(s)}</div>`).join('');
        return `<div style="background:rgba(255,255,255,0.03);border-radius:6px;padding:6px 8px;margin-top:6px;">
            <div style="font-size:12px;font-weight:700;color:#e5e7eb;">${icon} ${title}
                <span style="float:right;color:${scoreColor(d.score)};font-weight:700;">${d.score != null ? d.score + '分' : ''}</span></div>
            ${sig}${rsk}
        </div>`;
    }

    // ── 次日推演：贝叶斯因子表 + 三情景 ──
    function nextdayBlock(d) {
        if (!d) return '';
        const det = d.details || {};
        let factorHtml = '';
        const ft = det.factor_table || [];
        if (ft.length) {
            factorHtml = '<div style="margin-top:5px;font-size:10.5px;"><span class="text-muted">概率因子：</span>' +
                ft.map(f => {
                    const pct = Math.round((f.prob || 0) * 100);
                    const c = pct >= 45 ? '#73d13d' : pct >= 25 ? '#faad14' : '#ff7875';
                    return `<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:3px;background:rgba(255,255,255,0.06);">${esc(f.factor)} ${esc(f.value)} <b style="color:${c}">${pct}%</b></span>`;
                }).join('') + '</div>';
        }
        let scenHtml = '';
        const sc = det.scenarios || [];
        if (sc.length) {
            const scenColor = { '最强': '#73d13d', '中性': '#69c0ff', '最弱': '#ff7875' };
            scenHtml = '<div style="margin-top:6px;">' + sc.map(s => `
                <div style="font-size:10.5px;border-left:2px solid ${scenColor[s.scenario] || '#888'};padding-left:6px;margin-top:4px;">
                    <b style="color:${scenColor[s.scenario] || '#888'}">${esc(s.scenario)}情景</b>
                    <span class="text-muted">(${Math.round((s.probability || 0) * 100)}%)</span>：
                    ${esc(s.condition)}<br>
                    <span style="color:#ffd666;">→ ${esc(s.action)}</span>
                </div>`).join('') + '</div>';
        }
        const sig = (d.signals || []).slice(0, 2).map(s => `<div style="color:#73d13d;font-size:11px;line-height:1.5">✅ ${esc(s)}</div>`).join('');
        const rsk = (d.risks || []).slice(0, 3).map(s => `<div style="color:#ff7875;font-size:11px;line-height:1.5">⚠️ ${esc(s)}</div>`).join('');
        return `<div style="background:rgba(250,173,20,0.06);border:1px solid rgba(250,173,20,0.25);border-radius:6px;padding:6px 8px;margin-top:6px;">
            <div style="font-size:12px;font-weight:700;color:#ffd666;">🎲 次日确定性推演
                <span style="float:right;color:${scoreColor(d.score)};font-weight:700;">${d.score != null ? d.score + '分' : ''}</span></div>
            ${factorHtml}${scenHtml}${sig}${rsk}
        </div>`;
    }

    // 过滤提示：有标的被质量门槛剔除时显示
    let gateNotice = '';
    if (data.filtered_out && data.filtered_out > 0) {
        gateNotice = `<div style="font-size:11px;color:#faad14;background:rgba(250,173,20,0.08);border-radius:6px;padding:5px 10px;margin-bottom:8px;">
            🛡️ 已过滤 ${data.filtered_out} 只低确定性标的（${data.gate_rule || '综合分/题材/量价不达标'}），宁缺毋滥，仅展示 ${data.results.length} 只达标票</div>`;
    }

    let html = gateNotice + '<div class="row g-2">';
    data.results.forEach((r, idx) => {
        const gc = gradeColor[r.certainty_grade] || gradeColor.C;
        const bp = r.bayes_probability != null ? (r.bayes_probability * 100).toFixed(0) + '%' : '--';
        const pos = r.position_pct > 0 ? (r.position_pct * 100).toFixed(0) + '%' : '0%';
        const aIcon = actionIcon[r.action] || actionIcon[r.action_name] || '📊';
        const dd = r.dim_detail || {};
        const vp = dd.volume_price;
        const dims = [
            { label: '题材', val: r.theme_score },
            { label: '卡位', val: r.positioning_score },
            { label: '换手', val: r.turnover_score },
            { label: '封板', val: r.seal_quality_score },
            { label: '竞价', val: r.auction_score },
            { label: '次日', val: r.next_day_score }
        ];
        const dimBars = dims.map(d => {
            const c = scoreColor(d.val);
            return `<span style="display:inline-block;min-width:32px;"><small class="text-muted">${d.label}</small> `
                 + `<b style="color:${c}">${d.val || '--'}</b></span>`;
        }).join(' ');

        const vpLine = vp ? `<div style="font-size:10.5px;margin-top:3px;">
            <span style="color:${vp.grade === 'fail' ? '#ff7875' : vp.grade === 'caution' ? '#faad14' : '#73d13d'};">
            ${gradeIcon[vp.grade] || ''} 量价闸门 ${vp.grade === 'fail' ? '不通过' : vp.grade === 'caution' ? '谨慎' : '通过'}
            （${esc(vp.pattern || '')}）</span></div>` : '';

        const detailId = `ec-detail-${idx}`;
        const detailHtml = dd.theme ? `
            <div class="mt-2" style="border-top:1px dashed rgba(255,255,255,0.15);padding-top:6px;">
                ${vpLine}
                ${dimBlock('📚', '题材强弱', dd.theme)}
                ${dimBlock('🎯', '卡位分析', dd.position)}
                ${dimBlock('🔄', '换手结构', dd.turnover)}
                ${dimBlock('⏰', '竞价/盘口推演', dd.auction)}
                ${nextdayBlock(dd.nextday)}
            </div>` : '<div class="text-muted small mt-2">（旧数据无维度详情，执行一次全量分析后生成）</div>';

        html += `<div class="col-md-6 col-xl-4">
            <div class="card mb-0" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);">
              <div class="card-body p-2">
                <div class="d-flex justify-content-between align-items-center mb-1">
                  <div>
                    <span style="display:inline-block;padding:2px 8px;border-radius:4px;color:#000;font-weight:bold;font-size:11px;background:${gc};">${r.certainty_grade}</span>
                    <b class="text-light ms-2">${r.name || ''}</b>
                    <small class="text-muted">${r.boards || ''}板 ${r.concept || ''}</small>
                  </div>
                  <div class="text-end">
                    <div class="text-gold fw-bold">${r.composite_score || 0}分</div>
                    <small class="text-muted">次日${bp}</small>
                  </div>
                </div>
                <div class="mb-1" style="font-size:11px;">${dimBars}</div>
                <div class="d-flex justify-content-between align-items-center">
                  <span class="badge" style="background:rgba(250,173,20,0.2);color:#ffd666;">
                    ${aIcon} ${r.action_name || r.action || '--'}
                  </span>
                  <span class="badge" style="background:rgba(24,144,255,0.2);color:#69c0ff;">仓位 ${pos}</span>
                  <small class="text-muted">止损 ${r.stop_loss || '--'}</small>
                </div>
                <div style="margin-top:5px;">
                  <a href="javascript:void(0)" onclick="toggleEcDetail('${detailId}', this)"
                     style="font-size:11px;color:#69c0ff;text-decoration:none;">
                    ▸ 深度分析（题材·卡位·换手·竞价·次日推演）</a>
                </div>
                <div id="${detailId}" style="display:none;">${detailHtml}</div>
              </div>
            </div>
          </div>`;
    });
    html += '</div>';
    panel.innerHTML = html;
}

// 展开/收起进场确定性深度分析
function toggleEcDetail(id, el) {
    const box = document.getElementById(id);
    if (!box) return;
    const open = box.style.display !== 'none';
    box.style.display = open ? 'none' : 'block';
    el.innerHTML = open ? '▸ 深度分析（题材·卡位·换手·竞价·次日推演）'
                        : '▾ 收起深度分析';
}

// 变盘节点类型 → 颜色/图标
const TP_STYLE = {
    bottom:   { color: '#0ecb81', icon: '🌱', label: '冰点见底' },
    breakout: { color: '#00b8d9', icon: '🚀', label: '突破加速' },
    top:      { color: '#f6465d', icon: '⚠️', label: '高潮见顶' }
};

function renderSmashChart(chartData, tpData) {
    const dom = document.getElementById('smashChart');
    if (!dom) return;
    if (smashChartInstance) {
        smashChartInstance.dispose();
        smashChartInstance = null;
    }
    smashChartInstance = echarts.init(dom, null, { renderer: 'canvas' });

    const labels = chartData.map(d => d.date);
    const scData = chartData.map(d => d.value);
    const boardData = chartData.map(d => d.max_boards);

    const dragonByDate = {};
    if (tpData && Array.isArray(tpData.series)) {
        tpData.series.forEach(s => {
            if (s.dragon && s.dragon.name) {
                dragonByDate[s.date] = s.dragon;
            }
        });
    }

    const boardBarData = chartData.map(d => {
        const dragon = dragonByDate[d.date];
        return {
            value: d.max_boards,
            label: {
                show: !!dragon,
                position: 'top',
                formatter: dragon ? `${dragon.name}\n${dragon.level || ''}` : '',
                fontSize: 10,
                color: '#ffd700',
                fontWeight: 'bold',
                lineHeight: 13,
                textBorderColor: '#0f172a',
                textBorderWidth: 2
            }
        };
    });

    const scPoints = chartData.map(d => ({
        value: d.value,
        itemStyle: {
            color: d.value == null ? '#666'
                 : d.value < 3 ? '#0ecb81'
                 : d.value <= 5 ? '#fcd535'
                 : '#f6465d'
        }
    }));

    const signalMap = {};
    const birthSet = new Set();
    if (tpData) {
        (tpData.turning_points || []).forEach(tp => {
            if (!signalMap[tp.date]) signalMap[tp.date] = [];
            signalMap[tp.date].push(tp);
        });
        (tpData.dragon_birth_nodes || []).forEach(n => birthSet.add(n.date));
    }

    const marks = [];
    if (tpData) {
        (tpData.turning_points || []).forEach(tp => {
            if (tp.severity !== 'strong') return;
            const idx = labels.indexOf(tp.date);
            if (idx < 0) return;
            const colorMap = { bottom: '#0ecb81', breakout: '#00b8d9', top: '#f6465d' };
            const iconMap = { bottom: 'triangle', breakout: 'diamond', top: 'pin' };
            const labelMap = { bottom: '🌱', breakout: '🚀', top: '⚠️' };
            marks.push({
                name: tp.name,
                coord: [tp.date, scData[idx]],
                value: labelMap[tp.type] || '•',
                symbol: iconMap[tp.type] || 'circle',
                symbolSize: 24,
                itemStyle: { color: colorMap[tp.type] || '#999' },
                label: { show: true, fontSize: 11, color: '#fff' }
            });
        });
        (tpData.dragon_birth_nodes || []).forEach(n => {
            const idx = labels.indexOf(n.date);
            if (idx < 0) return;
            marks.push({
                name: '⭐' + n.dragon.name,
                coord: [n.date, scData[idx]],
                value: '⭐',
                symbol: 'path://M512 0l126.3 389.1 409.4-0.1-331.2 240.6 126.5 389.1L512 778.1 208.9 1018.7l126.5-389.1L4.3 389l409.4 0.1z',
                symbolSize: 42,
                symbolRotate: 0,
                itemStyle: { color: '#ffd700', borderColor: '#fff', borderWidth: 2 },
                label: { show: false }
            });
        });
    }

    smashChartInstance.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'axis',
            backgroundColor: 'rgba(15,23,42,0.96)',
            borderColor: '#f0b90b',
            borderWidth: 1,
            textStyle: { color: '#e5e7eb', fontSize: 12 },
            formatter: function (params) {
                if (!params || !params.length) return '';
                const idx = params[0].dataIndex;
                const d = labels[idx];
                const series = (tpData && tpData.series) ? tpData.series[idx] : null;
                let html = `<div style="font-weight:700;color:#f0b90b;margin-bottom:6px;">${d}${series ? ' · ' + series.phase : ''}</div>`;
                const drag = dragonByDate[d];
                if (drag) {
                    html += `<div style="margin-bottom:4px;color:#ffd700;font-weight:600;">🏆 总龙头：${drag.name}(${drag.code}) ${drag.level || ''}级 ${drag.boards || '?'}板 · ${drag.concept || ''}</div>`;
                }
                params.forEach(p => {
                    const v = (typeof p.value === 'object') ? p.value.value : p.value;
                    html += `<div>${p.marker} ${p.seriesName}: <b>${v}</b></div>`;
                });
                const sigs = signalMap[d];
                if (sigs && sigs.length) {
                    html += '<div style="margin-top:6px;border-top:1px solid rgba(255,255,255,0.1);padding-top:6px;">';
                    sigs.forEach(s => {
                        const c = s.type === 'bottom' ? '#0ecb81' : s.type === 'breakout' ? '#00b8d9' : '#f6465d';
                        html += `<div style="color:${c};">[${s.severity === 'strong' ? '强' : '中'}] ${s.name}</div>`;
                        html += `<div style="color:#94a3b8;font-size:11px;margin-bottom:4px;">${s.detail}</div>`;
                    });
                    html += '</div>';
                }
                if (birthSet.has(d)) {
                    const node = (tpData.dragon_birth_nodes || []).find(x => x.date === d);
                    if (node) {
                        html += `<div style="margin-top:4px;color:#ffd700;font-weight:700;">⭐ 新总龙头诞生：${node.trigger}</div>`;
                    }
                }
                return html;
            }
        },
        legend: {
            data: ['砸盘系数', '最高连板'],
            textStyle: { color: '#cbd5e1' },
            top: 0
        },
        grid: { left: 60, right: 60, top: 40, bottom: 70 },
        xAxis: {
            type: 'category',
            data: labels,
            axisLine: { lineStyle: { color: '#334155' } },
            axisLabel: { color: '#94a3b8', rotate: 45, fontSize: 11 }
        },
        yAxis: [
            {
                type: 'value',
                name: '砸盘系数',
                nameTextStyle: { color: '#f0b90b' },
                min: 0, max: 10,
                axisLine: { lineStyle: { color: '#f0b90b' } },
                axisLabel: { color: '#f0b90b' },
                splitLine: { lineStyle: { color: 'rgba(240,185,11,0.08)' } }
            },
            {
                type: 'value',
                name: '最高连板(板)',
                nameTextStyle: { color: '#00b8d9' },
                min: 0, max: 12, interval: 1,
                axisLine: { lineStyle: { color: '#00b8d9' } },
                axisLabel: { color: '#00b8d9' },
                splitLine: { show: false }
            }
        ],
        series: [
            {
                name: '最高连板',
                type: 'bar',
                yAxisIndex: 1,
                data: boardBarData,
                itemStyle: {
                    color: 'rgba(0,184,217,0.30)',
                    borderColor: 'rgba(0,184,217,0.85)',
                    borderWidth: 1
                },
                barWidth: '50%',
                z: 1
            },
            {
                name: '砸盘系数',
                type: 'line',
                yAxisIndex: 0,
                data: scPoints,
                smooth: true,
                symbol: 'circle',
                symbolSize: 9,
                lineStyle: { color: '#f0b90b', width: 2.5 },
                areaStyle: {
                    color: {
                        type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: 'rgba(240,185,11,0.35)' },
                            { offset: 1, color: 'rgba(240,185,11,0.02)' }
                        ]
                    }
                },
                markPoint: { data: marks, label: { fontSize: 12 } },
                z: 3
            }
        ]
    });

    window.addEventListener('resize', () => {
        if (smashChartInstance) smashChartInstance.resize();
    });
}

function renderTurningPointPanel(tpData) {
    const panelId = 'turning-point-panel';
    let panel = document.getElementById(panelId);
    if (!panel) {
        const chartDom = document.getElementById('smashChart');
        if (!chartDom) return;
        const hostCard = chartDom.closest('.card, .chart-card') || chartDom.parentElement;
        panel = document.createElement('div');
        panel.id = panelId;
        panel.style.marginTop = '16px';
        hostCard.appendChild(panel);
    }
    if (!tpData || !tpData.series) {
        panel.innerHTML = '';
        return;
    }

    const recent = (tpData.turning_points || []).slice().reverse().slice(0, 12);
    const births = (tpData.dragon_birth_nodes || []).slice().reverse().slice(0, 8);
    const s = tpData.summary || {};

    const phaseColor = {
        '冰点酝酿': '#0ecb81', '蓄力爬升': '#84cc16',
        '上升博弈': '#fcd535', '爆发高潮': '#f6465d',
        '崩塌退潮': '#a855f7', '震荡分化': '#9ca3af'
    }[s.current_phase] || '#9ca3af';

    const latestScColor = s.latest_sc < 3 ? '#0ecb81' : s.latest_sc <= 5 ? '#fcd535' : '#f6465d';

    const chipStyle = 'background:#1e293b;padding:6px 12px;border-radius:20px;font-size:0.85rem;color:#cbd5e1;';
    let html = `
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;color:#cbd5e1;">
            <span style="${chipStyle}">
                分析区间：<b style="color:#f0b90b;">${s.start_date || '--'}</b>
                <span style="color:#64748b;"> ~ </span><b style="color:#f0b90b;">${s.end_date || '--'}</b>
            </span>
            <span style="${chipStyle}">
                交易日：<b style="color:#00b8d9;">${s.days ?? 0}</b>
            </span>
            <span style="${chipStyle}">
                最新砸盘：<b style="color:${latestScColor};">${s.latest_sc ?? '--'}</b>
            </span>
            <span style="${chipStyle}">
                最新最高板：<b style="color:#00b8d9;">${s.latest_max_boards ?? '--'}板</b>
            </span>
            <span style="${chipStyle}">
                变盘节点：<b style="color:#f0b90b;">${s.turning_point_count ?? 0}</b>
            </span>
            <span style="${chipStyle}">
                总龙头诞生：<b style="color:#ffd700;">${s.dragon_birth_count ?? 0}</b>
            </span>
            <span style="${chipStyle}">
                当前周期：<b style="color:${phaseColor};">${s.current_phase || '--'}</b>
            </span>
        </div>`;

    if (births.length) {
        html += `<div style="color:#ffd700;font-weight:600;margin:6px 0 8px;font-size:0.95rem;">⭐ 总龙头诞生节点（命中后自动微信推送）</div>`;
        html += `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:14px;">`;
        births.forEach(n => {
            const d = n.dragon;
            const lvlCls = ({ SS: 'background:#f6465d;color:#fff;', S: 'background:#f0b90b;color:#0b1020;', A: 'background:#00b8d9;color:#fff;', B: 'background:#64748b;color:#fff;' })[d.level] || 'background:#64748b;color:#fff;';
            html += `<div style="background:linear-gradient(90deg,rgba(255,215,0,0.10),rgba(255,215,0,0.02));border-left:4px solid #ffd700;border-radius:10px;padding:12px 14px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                    <span style="color:#ffd700;font-weight:700;font-size:0.92rem;">${n.date}</span>
                    <span style="padding:2px 10px;border-radius:12px;font-size:0.75rem;font-weight:700;${lvlCls}">${d.level}级 · ${d.score}分</span>
                </div>
                <div style="font-size:1.02rem;font-weight:600;color:#fff;">${d.name} <span style="color:#94a3b8;font-size:0.82rem;font-weight:400;">${d.code}</span></div>
                <div style="color:#94a3b8;font-size:0.82rem;line-height:1.6;margin-top:2px;">
                    ${d.boards}板 · ${d.lifecycle} · 概念：${d.concept || '--'}<br>
                    大盘：${n.phase} · 砸盘${n.sc} · 最高${n.max_boards}板
                </div>
                <div style="color:#84cc16;font-size:0.82rem;margin-top:6px;">🎯 ${n.trigger}</div>
            </div>`;
        });
        html += `</div>`;
    }

    if (recent.length) {
        html += `<div style="color:#e5e7eb;font-weight:600;margin:10px 0 6px;font-size:0.95rem;">📊 近期变盘节点明细（空仓信号将通过微信提醒）</div>`;
        html += `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.84rem;color:#cbd5e1;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">日期</th>
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">类型</th>
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">强度</th>
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">信号</th>
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">细节</th>
                <th style="padding:8px 10px;text-align:left;color:#f0b90b;">当日龙头</th>
            </tr></thead><tbody>`;
        recent.forEach(tp => {
            const typeMap = {
                bottom: ['🌱 冰点见底', 'rgba(14,203,129,0.2)', '#0ecb81'],
                breakout: ['🚀 突破加速', 'rgba(0,184,217,0.2)', '#00b8d9'],
                top: ['⚠️ 见顶空仓', 'rgba(246,70,93,0.2)', '#f6465d']
            };
            const [typeLabel, bg, fg] = typeMap[tp.type] || ['--', 'transparent', '#999'];
            const sevLabel = tp.severity === 'strong' ? '强信号' : '中信号';
            const sevBg = tp.severity === 'strong' ? 'rgba(240,185,11,0.2)' : 'rgba(100,116,139,0.3)';
            const sevFg = tp.severity === 'strong' ? '#f0b90b' : '#cbd5e1';
            const dragonCell = tp.dragon_name
                ? `<span style="color:#f0b90b;">🏆 ${tp.dragon_name}</span> <span style="color:#64748b;">(${tp.dragon_level})</span>`
                : (tp.type === 'top' ? '<span style="color:#f6465d;">建议空仓</span>' : '--');
            html += `<tr style="border-bottom:1px solid rgba(255,255,255,0.06);">
                <td style="padding:8px 10px;color:#94a3b8;white-space:nowrap;">${tp.date}</td>
                <td style="padding:8px 10px;"><span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.75rem;font-weight:600;background:${bg};color:${fg};">${typeLabel}</span></td>
                <td style="padding:8px 10px;"><span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.75rem;font-weight:600;background:${sevBg};color:${sevFg};">${sevLabel}</span></td>
                <td style="padding:8px 10px;color:#e5e7eb;font-weight:600;">${tp.name}</td>
                <td style="padding:8px 10px;color:#94a3b8;">${tp.detail}</td>
                <td style="padding:8px 10px;white-space:nowrap;">${dragonCell}</td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }

    panel.innerHTML = html;
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
                    if (d.status === 'completed') setTimeout(() => { loadDashboard(); loadDailyLatest(); }, 1000);
                }
            } catch (e) { console.error(e); }
        }, 2000);
    } catch (e) {
        document.getElementById('daily-status-text').textContent = '❌ ' + e.message;
        btn.disabled = false; btn.innerHTML = '<i class="bi bi-cloud-download"></i> 获取今日数据';
    }
}

// ============ 每日分析（自动加载结构化数据） ============
let dailyPollTimer = null, conceptBarInstance = null;
let _dailyCache = null;

async function loadDailyLatest() {
    const empty = document.getElementById('daily-empty');
    const results = document.getElementById('daily-results');
    empty.style.display = 'none';
    try {
        const resp = await fetchJSON('/api/daily/latest');
        if (!resp.success) {
            empty.style.display = 'block';
            empty.querySelector('p').textContent = resp.error || '加载失败';
            return;
        }
        const data = resp.data;
        if (!data.has_data) {
            empty.style.display = 'block';
            empty.querySelector('p').textContent = data.message || '暂无数据';
            results.style.display = 'none';
            return;
        }
        _dailyCache = data;
        renderDailyData(data);
    } catch (e) {
        console.error('daily latest error:', e);
        empty.style.display = 'block';
    }
}

function renderDailyData(data) {
    document.getElementById('daily-results').style.display = 'block';
    document.getElementById('daily-data-date').textContent = `数据日期: ${data.date || '--'}`;

    const s = data.summary || {};
    const smash = data.smash || {};
    // 核心指标卡
    const metricsHtml = [
        { label: '涨停', val: s.limit_up_count ?? '--', color: 'text-gold' },
        { label: '跌停', val: s.limit_down_count ?? '--', color: 'text-danger' },
        { label: '炸板率', val: s.explosion_rate != null ? (s.explosion_rate * 100).toFixed(1) + '%' : '--', color: '' },
        { label: '最高连板', val: (s.max_continuous_boards ?? '--') + '板', color: 'text-info' },
        { label: '砸盘系数', val: smash.smash_coefficient != null ? Number(smash.smash_coefficient).toFixed(2) : '--', color: smash.smash_coefficient < 3 ? 'text-success' : smash.smash_coefficient <= 5 ? 'text-warning' : 'text-danger' },
        { label: '市场热度', val: s.market_heat != null ? Number(s.market_heat).toFixed(0) : '--', color: '' },
    ].map(m => `<div class="col-4 col-md-2"><div class="card card-dark"><div class="card-body text-center py-2"><div class="card-label" style="font-size:0.7rem">${m.label}</div><div class="card-value ${m.color}" style="font-size:1.2rem">${m.val}</div></div></div></div>`).join('');
    document.getElementById('daily-metric-cards').innerHTML = metricsHtml;

    // 资金流
    renderDailyCapitalFlow(data.capital_flow);

    // 龙头
    renderDailyDragons(data.dragons || []);

    // 进场确定性
    renderDailyEntryCertainty(data.entry_certainty || []);

    // 操作计划
    renderDailyPlans(data.operation_plans || []);

    // 连板梯队
    renderDailyBoardTiers(data.board_tiers || []);

    // 概念热度
    renderConceptBar(data.concept_heat || []);
}

function renderDailyCapitalFlow(cf) {
    const sec = document.getElementById('daily-capital-flow-section');
    if (!cf) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';

    const levelMap = {
        aggressive: { name: '积极', color: '#f6465d', bg: 'rgba(246,70,93,0.15)' },
        positive: { name: '偏多', color: '#faad14', bg: 'rgba(250,173,20,0.15)' },
        neutral: { name: '中性', color: '#9ca3af', bg: 'rgba(156,163,175,0.15)' },
        cautious: { name: '谨慎', color: '#1890ff', bg: 'rgba(24,144,255,0.15)' },
        defensive: { name: '防守', color: '#0ecb81', bg: 'rgba(14,203,129,0.15)' },
    };
    const lv = levelMap[cf.composite_level] || levelMap.neutral;

    document.getElementById('daily-cf-summary').innerHTML = `
        <span style="display:inline-block;padding:4px 14px;border-radius:20px;background:${lv.bg};color:${lv.color};font-weight:700;font-size:0.85rem">
            综合 ${cf.composite_score || 0}分 · ${lv.name} · 仓位${cf.position_multiplier || 1}x
        </span>`;

    const dims = [
        { key: 'attack', icon: '⚔️', label: '进攻力度', score: cf.attack_score, level: cf.attack_level, metrics: cf.attack_metrics },
        { key: 'persistence', icon: '🔗', label: '持续能力', score: cf.persistence_score, level: cf.persistence_level, metrics: cf.persistence_metrics },
        { key: 'rotation', icon: '🔄', label: '轮动模式', score: cf.rotation_score, level: cf.rotation_pattern, metrics: cf.rotation_metrics, isPattern: true },
    ];
    const levelNameMap = {
        strong: '强攻', moderate: '温和', weak: '弱攻', defensive: '防守',
        mainline: '主线主导', rotation: '板块轮动', diffusion: '全面扩散',
        contraction: '收缩防守', chaos: '无序轮动',
    };
    let dimHtml = '';
    dims.forEach(d => {
        const score = d.score || 0;
        const pct = Math.min(100, score);
        const barColor = score >= 70 ? '#0ecb81' : score >= 50 ? '#fcd535' : score >= 30 ? '#faad14' : '#f6465d';
        let detail = '';
        if (d.metrics) {
            const m = d.metrics;
            if (d.key === 'attack') {
                detail = `涨停${m.total_limit_up || 0}家 · 封板率${((m.seal_rate || 0) * 100).toFixed(0)}% · 早盘占比${((m.early_ratio || 0) * 100).toFixed(0)}%`;
            } else if (d.key === 'persistence') {
                detail = `晋级率${((m.avg_promotion_rate || 0) * 100).toFixed(0)}% · 高标存活${((m.high_survival_rate || 0) * 100).toFixed(0)}%`;
            } else if (d.key === 'rotation') {
                detail = `主线: ${m.top_concept || '--'}(${m.top_concept_count || 0}家·${m.top_concept_days || 0}天) · Top3集中度${((m.top3_ratio || 0) * 100).toFixed(0)}%`;
            }
        }
        dimHtml += `<div class="col-md-4">
            <div style="background:rgba(255,255,255,0.03);border-radius:8px;padding:12px">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span style="font-weight:600;color:#e5e7eb;font-size:0.88rem">${d.icon} ${d.label}</span>
                    <span style="color:${barColor};font-weight:700;font-size:0.85rem">${score}分</span>
                </div>
                <div class="progress mb-1" style="height:5px"><div class="progress-bar" style="width:${pct}%;background:${barColor}"></div></div>
                <small class="text-muted" style="font-size:0.72rem">${levelNameMap[d.level] || d.level || '--'} · ${detail}</small>
            </div>
        </div>`;
    });
    document.getElementById('daily-cf-dimensions').innerHTML = dimHtml;

    // 组合信号
    let sigHtml = '';
    if (cf.combo_signals && Array.isArray(cf.combo_signals)) {
        sigHtml = cf.combo_signals.map(s => {
            if (typeof s === 'object' && s.text) {
                return `<span style="display:inline-block;padding:3px 10px;margin:2px;border-radius:12px;background:rgba(240,185,11,0.1);color:#ffd666;font-size:0.75rem">${s.emoji || ''} ${s.text}</span>`;
            }
            return '';
        }).join('');
    }
    if (cf.guidance) {
        sigHtml += `<div class="text-muted small mt-1" style="padding-left:4px">📌 ${cf.guidance}</div>`;
    }
    document.getElementById('daily-cf-signals').innerHTML = sigHtml;
}

function renderDailyDragons(dragons) {
    const sec = document.getElementById('daily-dragons-section');
    const list = document.getElementById('daily-dragons-list');
    if (!dragons.length) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';

    const typeMap = {
        total_dragon: { name: '总龙头', color: '#f6465d' },
        sector_dragon: { name: '板块龙', color: '#faad14' },
        catch_up_dragon: { name: '补涨龙', color: '#fcd535' },
        switch_dragon: { name: '切换龙', color: '#0ecb81' },
    };
    const levelColor = { SS: '#f6465d', S: '#faad14', A: '#1890ff', B: '#8c8c8c' };
    const lifecycleMap = { launch: '🚀启动', acceleration: '⚡加速', climax: '🔥高潮', decline: '📉衰退' };

    list.innerHTML = dragons.slice(0, 6).map(d => {
        const tp = typeMap[d.dragon_type] || { name: d.dragon_type || '', color: '#999' };
        const lc = levelColor[d.certainty_level] || '#999';
        const lf = lifecycleMap[d.lifecycle_stage] || d.lifecycle_stage;
        const scores = d;
        return `<div style="padding:10px;margin-bottom:8px;background:rgba(255,255,255,0.03);border-radius:8px;border-left:3px solid ${lc}">
            <div class="d-flex justify-content-between align-items-center mb-1">
                <div>
                    <span style="padding:1px 7px;border-radius:3px;background:${lc};color:#000;font-weight:700;font-size:0.7rem">${d.certainty_level}</span>
                    <b class="text-light ms-1" style="font-size:0.9rem">${d.name || '--'}</b>
                    <small class="text-muted">${d.code}</small>
                </div>
                <span class="text-gold fw-bold">${Number(d.total_score || 0).toFixed(0)}分</span>
            </div>
            <div class="d-flex gap-2 flex-wrap" style="font-size:0.72rem;color:#9ca3af">
                <span style="color:${tp.color}">${tp.name}</span>
                <span>${lf}</span>
                <span>${d.limit_up_days || '?'}板</span>
                <span>封单${((d.seal_ratio || 0) * 100).toFixed(1)}%</span>
                ${d.concept ? `<span>· ${d.concept.split(';')[0]}</span>` : ''}
            </div>
        </div>`;
    }).join('');
}

function renderDailyEntryCertainty(items) {
    const sec = document.getElementById('daily-entry-section');
    const list = document.getElementById('daily-entry-list');
    if (!items.length) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';

    const gradeColor = {
        'S+': 'linear-gradient(135deg,#ff4d4f,#faad14)', 'S': 'linear-gradient(135deg,#faad14,#ffd666)',
        'A': 'linear-gradient(135deg,#1890ff,#69c0ff)', 'B': 'linear-gradient(135deg,#52c41a,#95de64)',
        'C': 'linear-gradient(135deg,#8c8c8c,#bfbfbf)', 'D': 'linear-gradient(135deg,#595959,#8c8c8c)',
    };
    list.innerHTML = items.slice(0, 6).map(r => {
        const gc = gradeColor[r.certainty_grade] || gradeColor.C;
        const bp = r.bayes_probability != null ? (r.bayes_probability * 100).toFixed(0) + '%' : '--';
        const pos = r.position_pct > 0 ? (r.position_pct * 100).toFixed(0) + '%' : '0%';
        return `<div style="padding:8px 10px;margin-bottom:6px;background:rgba(255,255,255,0.03);border-radius:6px;display:flex;justify-content:space-between;align-items:center;gap:8px">
            <div style="min-width:0;flex:1">
                <span style="display:inline-block;padding:1px 6px;border-radius:3px;color:#000;font-weight:700;font-size:0.68rem;background:${gc};">${r.certainty_grade}</span>
                <b class="text-light ms-1" style="font-size:0.85rem">${r.name || ''}</b>
                <small class="text-muted">${r.boards || ''}板</small>
                ${r.concept ? `<small class="text-muted d-block">${r.concept}</small>` : ''}
            </div>
            <div class="text-end" style="white-space:nowrap">
                <div class="text-gold fw-bold" style="font-size:0.85rem">${Number(r.composite_score || 0).toFixed(0)}分</div>
                <small class="text-muted">次日${bp} · 仓${pos}</small>
            </div>
        </div>`;
    }).join('');
}

function renderDailyPlans(plans) {
    const sec = document.getElementById('daily-plans-section');
    const list = document.getElementById('daily-plans-list');
    if (!plans.length) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';

    const strategyMap = { board_hit: '🎯打板', half_way: '🛤️半路', low_buy: '💰低吸', wait: '⏸️观望' };
    const levelColor = { SS: '#f6465d', S: '#faad14', A: '#1890ff', B: '#8c8c8c' };

    list.innerHTML = plans.slice(0, 6).map(p => {
        const lc = levelColor[p.certainty_level] || '#999';
        const details = p.plan_details || {};
        const pos = p.position_pct != null ? (p.position_pct * 100).toFixed(1) + '%' : '--';
        const priceLow = p.buy_price_low || 0;
        const priceHigh = p.buy_price_high || 0;
        const stopLoss = p.stop_loss_price || 0;
        const rr = p.risk_reward_ratio || 0;
        const expRet = p.expected_return || 0;
        const conditions = (details.buy_conditions || []).slice(0, 2);
        const stopRules = (details.stop_rules || []).slice(0, 1);

        return `<div class="col-md-6 col-lg-4">
            <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:12px;border-top:3px solid ${lc}">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <div>
                        <span style="padding:1px 6px;border-radius:3px;background:${lc};color:#000;font-weight:700;font-size:0.7rem">${p.certainty_level || '--'}</span>
                        <b class="text-light ms-1">${p.name || '--'}</b>
                        <small class="text-muted">${p.code || ''}</small>
                    </div>
                </div>
                <div class="row g-1 mb-2" style="font-size:0.75rem">
                    <div class="col-6"><span class="text-muted">策略:</span> <span class="text-light">${strategyMap[p.buy_strategy] || p.buy_strategy || '--'}</span></div>
                    <div class="col-6"><span class="text-muted">仓位:</span> <span class="text-gold fw-bold">${pos}</span></div>
                    <div class="col-6"><span class="text-muted">买入:</span> <span class="text-light">${priceLow ? priceLow.toFixed(2) + '~' + priceHigh.toFixed(2) : '--'}</span></div>
                    <div class="col-6"><span class="text-muted">止损:</span> <span class="text-danger">${stopLoss ? stopLoss.toFixed(2) : '--'}</span></div>
                    <div class="col-6"><span class="text-muted">盈亏比:</span> <span style="color:${rr >= 2 ? '#0ecb81' : '#faad14'}">${rr ? rr.toFixed(1) : '--'}</span></div>
                    <div class="col-6"><span class="text-muted">期望收益:</span> <span style="color:${expRet >= 0 ? '#0ecb81' : '#f6465d'}">${expRet ? expRet.toFixed(1) + '%' : '--'}</span></div>
                </div>
                ${conditions.length ? `<div style="font-size:0.7rem;color:#9ca3af;margin-bottom:3px">⏰ ${conditions.join('；')}</div>` : ''}
                ${stopRules.length ? `<div style="font-size:0.7rem;color:#f6465d">🛑 ${stopRules[0]}</div>` : ''}
            </div>
        </div>`;
    }).join('');
}

function renderDailyBoardTiers(tiers) {
    const container = document.getElementById('board-tiers');
    if (!tiers.length) { container.innerHTML = '<div class="text-muted small">暂无数据</div>'; return; }
    container.innerHTML = tiers.map(t => {
        const stocks = (t.stocks || '').split(',').filter(Boolean);
        const color = t.limit_up_days >= 5 ? '#f6465d' : t.limit_up_days >= 3 ? '#faad14' : t.limit_up_days >= 2 ? '#1890ff' : '#8c8c8c';
        return `<div style="display:flex;align-items:baseline;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.04)">
            <span style="min-width:40px;padding:2px 8px;border-radius:4px;background:${color}20;color:${color};font-weight:700;font-size:0.75rem;text-align:center">${t.limit_up_days}板</span>
            <span class="text-muted small me-2">${t.cnt}只</span>
            <span style="font-size:0.78rem;color:#d1d5db;line-height:1.6">${stocks.slice(0, 12).join('、')}${stocks.length > 12 ? '...' : ''}</span>
        </div>`;
    }).join('');
}

async function runDailyAnalysis() {
    const btn = document.getElementById('btn-run-daily');
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 执行中...';
    document.getElementById('daily-progress').style.display = 'block';
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
        if (d.status === 'completed') {
            clearInterval(dailyPollTimer);
            document.getElementById('daily-spinner').style.display = 'none';
            resetDailyBtn();
            document.getElementById('daily-progress').style.display = 'none';
            // 重新从数据库加载结构化结果
            setTimeout(() => loadDailyLatest(), 1000);
        } else if (d.status === 'error') {
            clearInterval(dailyPollTimer); resetDailyBtn();
            document.getElementById('daily-progress').style.display = 'none';
        }
    }, 2000);
}
function resetDailyBtn() { const btn = document.getElementById('btn-run-daily'); btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> 重新执行全量分析'; }

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

// ============ V4: 智能推荐（结构化展示） ============
let radarChartInstance = null;
let _recommendCache = null;
const dimLabels = { 'concept_heat': '概念热度', 'board_position': '连板位置', 'seal_quality': '封板质量', 'cap_fit': '市值适配', 'volume_price': '量价走势', 'dragon_bonus': '龙头加成' };

function setRecLoading(loading, text = '加载中...') {
    const progress = document.getElementById('recommend-progress');
    const bar = document.getElementById('rec-progress-bar');
    const status = document.getElementById('rec-status-text');
    const spinner = document.getElementById('rec-spinner');
    if (!progress) return;
    progress.style.display = loading ? 'block' : 'none';
    if (bar) bar.style.width = loading ? '60%' : '0%';
    if (status) status.textContent = text;
    if (spinner) spinner.style.display = loading ? 'inline-block' : 'none';
}

async function runRecommend() {
    const btn = document.getElementById('btn-run-recommend');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 刷新中...';
    }
    setRecLoading(true, '正在刷新智能推荐...');
    await loadRecommendLatest(false);
    setRecLoading(false);
    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> 刷新推荐';
    }
}

async function loadRecommendLatest(showLoading = true) {
    const empty = document.getElementById('recommend-empty');
    const results = document.getElementById('recommend-results');
    if (empty) empty.style.display = 'none';
    if (showLoading) setRecLoading(true, '正在加载推荐数据...');
    try {
        const resp = await fetchJSON('/api/recommend/latest');
        if (!resp.success) {
            if (results) results.style.display = 'none';
            if (empty) {
                empty.style.display = 'block';
                empty.querySelector('p').textContent = resp.error || '加载失败';
            }
            return;
        }
        const data = resp.data;
        if (!data.has_data) {
            if (results) results.style.display = 'none';
            if (empty) {
                empty.style.display = 'block';
                empty.querySelector('p').textContent = data.message || '暂无推荐数据';
            }
            return;
        }
        _recommendCache = data;
        renderRecommendData(data);
    } catch (e) {
        console.error('recommend latest error:', e);
        if (results) results.style.display = 'none';
        if (empty) {
            empty.style.display = 'block';
            empty.querySelector('p').textContent = '推荐数据加载失败';
        }
    } finally {
        if (showLoading) setRecLoading(false);
    }
}

function getConfidenceBadge(level) {
    const colors = {
        'SS': 'background:linear-gradient(135deg,#f6465d,#f0b90b);color:#fff;font-weight:800',
        'S':  'background:linear-gradient(135deg,#f0b90b,#ffd666);color:#111827;font-weight:800',
        'A':  'background:rgba(14,203,129,0.18);color:#0ecb81;font-weight:700',
        'B':  'background:rgba(34,211,238,0.15);color:#22d3ee;font-weight:600',
        'C':  'background:rgba(156,163,175,0.18);color:#d1d5db',
    };
    const names = {
        'SS': 'SS级·极高确定性',
        'S':  'S级·高确定性',
        'A':  'A级·较高确定性',
        'B':  'B级·可关注',
        'C':  'C级·观察',
    };
    const style = colors[level] || colors.C;
    return `<span class="rec-stock-tag" style="${style};border:1px solid;border-radius:4px;padding:2px 8px;font-size:0.75rem">${names[level] || (level || 'C') + '级'}</span>`;
}

function normalizeDimScores(scores) {
    return {
        concept_heat: Number(scores?.concept_heat ?? 0),
        board_position: Number(scores?.board_position ?? 0),
        seal_quality: Number(scores?.seal_quality ?? 0),
        cap_fit: Number(scores?.cap_fit ?? 0),
        volume_price: Number(scores?.volume_price ?? 0),
    };
}

function renderRecommendData(data) {
    document.getElementById('recommend-results').style.display = 'block';
    document.getElementById('rec-data-date').textContent =
        `数据日期: ${data.date || '--'}${data.target_date ? ` · 预测 ${data.target_date}` : ''}`;

    const ms = data.market_state || {};
    const smash = ms.smash_coefficient;
    const marketHtml = [
        { label: '周期阶段', val: ms.cycle_phase || '--', cls: 'text-gold' },
        { label: '砸盘系数', val: formatNumber(smash), cls: getSmashColor(smash) },
        { label: '炸板率', val: formatPercent(ms.explosion_rate), cls: '' },
        { label: '涨停/跌停', val: `${ms.limit_up_count ?? '--'} / ${ms.limit_down_count ?? '--'}`, cls: '' },
        { label: '最高连板', val: ms.max_boards != null ? ms.max_boards + '板' : '--', cls: 'text-info' },
        { label: '市场情绪', val: ms.sentiment || '--', cls: '' },
    ].map(item => `<div class="market-state-pill"><span>${item.label}</span><b class="${item.cls}">${item.val}</b></div>`).join('');

    const hotConcepts = Array.isArray(ms.hot_concepts_top5) ? ms.hot_concepts_top5.filter(Boolean) : [];
    const _adviceText = (typeof ms.action_advice === 'object' && ms.action_advice) ? (ms.action_advice.advice_text || '') : (ms.action_advice || '');
    const actionAdvice = _adviceText ? `<div class="action-advice"><i class="bi bi-crosshair"></i> ${_adviceText}</div>` : '';
    document.getElementById('rec-market-state').innerHTML = marketHtml + actionAdvice +
        (hotConcepts.length ? `<div class="hot-concept-line"><span>热门概念</span><b>${hotConcepts.join('、')}</b></div>` : '');

    const ns = data.next_day_strategy || {};
    document.getElementById('rec-next-strategy').innerHTML = `
        <div class="strategy-section"><div class="strategy-label"><i class="bi bi-compass"></i> 整体策略</div><div class="strategy-text">${ns.overall_strategy || '--'}</div></div>
        <div class="row g-2 mb-2">
            <div class="col-5"><div class="mini-stat"><span>目标高度</span><b>${ns.target_board_height || '--'}</b></div></div>
            <div class="col-7"><div class="mini-stat"><span>关注概念</span><b style="font-size:0.78rem">${(ns.focus_concepts || []).join('、') || '--'}</b></div></div>
        </div>
        <div class="strategy-section risk-text"><div class="strategy-label"><i class="bi bi-shield-exclamation"></i> 风控要点</div><div class="strategy-text">${ns.risk_control || '--'}</div></div>
    `;

    // 各子模块独立容错：单个模块渲染异常不影响其他模块，更不应触发整页"加载失败"
    const _safe = (fn, name) => { try { fn(); } catch (e) { console.error(`推荐页[${name}]渲染异常:`, e); } };
    _safe(() => renderRecommendCapitalFlow(data.capital_flow), '资金流');
    _safe(() => renderRecommendStocks(data.recommendations || [], data.operation_plans || {}, data.entry_certainty || []), '推荐个股');
    _safe(() => renderRadarChart(data.recommendations || []), '雷达图');
    _safe(() => renderRecEntryMini(data.entry_certainty || []), '进场迷你卡');
    _safe(() => loadExitSignals(), '出场信号');
}

function renderRecommendCapitalFlow(cf) {
    const bar = document.getElementById('rec-cf-bar');
    const content = document.getElementById('rec-cf-content');
    if (!bar || !content) return;
    if (!cf) { bar.style.display = 'none'; return; }
    bar.style.display = 'block';

    const levelMap = {
        aggressive: { name: '积极进攻', color: '#f6465d', bg: 'rgba(246,70,93,0.14)' },
        positive: { name: '偏多', color: '#faad14', bg: 'rgba(250,173,20,0.14)' },
        neutral: { name: '中性', color: '#d1d5db', bg: 'rgba(209,213,219,0.10)' },
        cautious: { name: '谨慎', color: '#38bdf8', bg: 'rgba(56,189,248,0.12)' },
        defensive: { name: '防守', color: '#0ecb81', bg: 'rgba(14,203,129,0.12)' },
    };
    const lv = levelMap[cf.composite_level] || levelMap.neutral;
    const signals = Array.isArray(cf.combo_signals) ? cf.combo_signals.map(s => typeof s === 'string' ? s : (s.text || '')).filter(Boolean).slice(0, 3) : [];
    content.innerHTML = `
        <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
            <div><i class="bi bi-cash-stack text-gold"></i> <b>资金流状态</b></div>
            <span class="cf-badge" style="background:${lv.bg};color:${lv.color};border-color:${lv.color}55">${lv.name} · ${Number(cf.composite_score || 0).toFixed(0)}分 · 仓位${cf.position_multiplier || 1}x</span>
        </div>
        ${signals.length ? `<div class="cf-signals">${signals.map(s => `<span>${s}</span>`).join('')}</div>` : ''}
    `;
}

function renderRecommendStocks(recs, plansMap, entryList) {
    const list = document.getElementById('rec-stock-list');
    if (!list) return;
    const entryMap = {};
    (entryList || []).forEach(e => { if (e.code) entryMap[e.code] = e; });
    const sorted = [...recs].sort((a, b) => Number(b.total_score || 0) - Number(a.total_score || 0));

    if (!sorted.length) {
        list.innerHTML = `
            <div class="empty-block danger">
                <div style="font-size:2rem">🛡️</div>
                <b>当前无高确定性标的</b>
                <span>市场条件不满足信心等级要求，建议空仓观望；系统宁可错过，不做低确定性交易。</span>
            </div>`;
        return;
    }

    list.innerHTML = sorted.map((r, idx) => {
        const level = r.confidence_level || r.grade || 'C';
        const scores = normalizeDimScores(r.dimension_scores);
        const code = r.code || '';
        const borderColor = level === 'SS' ? '#f6465d' : level === 'S' ? '#f0b90b' : level === 'A' ? '#0ecb81' : '#22d3ee';
        const winRate = Number(r.historical_win_rate || r.win_rate || 0);
        const winPct = winRate > 0 && winRate <= 1 ? (winRate * 100).toFixed(0) + '%' : (winRate > 1 ? winRate.toFixed(0) + '%' : '--');
        const concept = r.concept || '未知概念';
        const concepts = concept.split(/[;；,，、]/).filter(Boolean).slice(0, 3);
        const dimBarsHtml = Object.entries(scores).map(([dim, val]) => {
            const color = val >= 75 ? '#0ecb81' : val >= 55 ? '#fcd535' : val >= 35 ? '#faad14' : '#f6465d';
            return `<div class="dim-score-bar"><span class="dim-score-label">${dimLabels[dim] || dim}</span><div class="dim-score-track"><div class="dim-score-fill" style="width:${Math.min(100, val)}%;background:${color}"></div></div><span class="dim-score-val">${Number(val || 0).toFixed(0)}</span></div>`;
        }).join('');

        const risks = Array.isArray(r.risk_notes) ? r.risk_notes.filter(Boolean) : [];
        const reasons = r.dimension_reasons || {};
        const reasonChips = Object.entries(reasons).slice(0, 3).map(([k, v]) => `<span>${dimLabels[k] || k}：${v}</span>`).join('');
        const plan = plansMap[code] || entryMap[code];
        const planHtml = plan ? renderPlanDetails(plan) : '';

        // 量价走势闸门标签（首要依据，置顶醒目）
        let vpTag = '';
        if (r.vp_pattern) {
            const vpColor = r.vp_grade === 'pass' ? '#0ecb81'
                : r.vp_grade === 'caution' ? '#faad14' : '#f6465d';
            const vpIcon = r.vp_grade === 'pass' ? '🟢' : r.vp_grade === 'caution' ? '🟡' : '🔴';
            const vetoTxt = Array.isArray(r.vp_veto) && r.vp_veto.length ? ` · ${r.vp_veto[0].slice(0, 40)}` : '';
            vpTag = `<div class="condition-match" style="border-left:3px solid ${vpColor};margin-top:6px;background:rgba(255,255,255,0.02)">
                <i class="bi bi-graph-up-arrow"></i> <b style="color:${vpColor}">${vpIcon}量价(首要)：${r.vp_pattern}</b>
                <span style="color:#9ca3af;font-size:0.72rem"> ${r.vp_gate || ''}${vetoTxt}</span></div>`;
        }

        // 分歧/一致节奏标签
        let divergenceTag = '';
        const divLabel = r.divergence_label || '';
        const divState = r.divergence_state || '';
        if (divLabel) {
            const divColor = divState === 'divergence_to_consensus' ? '#0ecb81'
                : divState === 'consensus' && r.is_yizi && Number(r.limit_up_days || 1) >= 4 ? '#f6465d'
                : divState === 'consensus' ? '#faad14'
                : divState === 'high_divergence' ? '#f6465d'
                : '#ff9800';
            divergenceTag = `<div class="condition-match" style="border-left:3px solid ${divColor};margin-top:6px"><i class="bi bi-signal"></i> <b style="color:${divColor}">${divLabel}</b></div>`;
        }

        return `
        <div class="rec-stock-card" style="border-left:3px solid ${borderColor}">
            <div class="rec-stock-header">
                <div>
                    <span class="rec-stock-name">${idx + 1}. ${r.name || '--'}</span>
                    <span class="rec-stock-code">${code}</span>
                    ${getConfidenceBadge(level)}
                    ${r.is_yizi ? '<span class="rec-stock-tag" style="background:rgba(246,70,93,0.18);color:#f6465d;border-color:#f6465d55">一字板</span>' : ''}
                </div>
                <div class="rec-stock-score">${Number(r.total_score || 0).toFixed(1)}</div>
            </div>
            ${r.condition_match ? `<div class="condition-match"><i class="bi bi-patch-check-fill"></i> ${r.condition_match} · 历史胜率 ${winPct}</div>` : ''}
            ${vpTag}
            ${divergenceTag}
            <div class="rec-stock-meta">
                ${concepts.map(c => `<span class="rec-stock-tag">${c}</span>`).join('')}
                <span class="rec-stock-tag">${r.limit_up_days || 1}连板</span>
                <span class="rec-stock-tag action">${r.suggested_action || '观望'}</span>
            </div>
            <div class="row g-3">
                <div class="col-md-6">${dimBarsHtml}</div>
                <div class="col-md-6">
                    <div class="rec-stock-reason"><i class="bi bi-lightbulb"></i> ${r.reason || '暂无详细理由'}</div>
                    ${reasonChips ? `<div class="dimension-reasons">${reasonChips}</div>` : ''}
                    ${risks.length ? `<div class="rec-stock-risks"><i class="bi bi-exclamation-triangle"></i> ${risks.join('；')}</div>` : ''}
                </div>
            </div>
            ${planHtml}
        </div>`;
    }).join('');
}

function renderPlanDetails(plan) {
    if (!plan) return '';
    const details = plan.plan_details || {};
    const strategyMap = { board_hit: '🎯打板', half_way: '🛤️半路', low_buy: '💰低吸', wait: '⏸️观望' };
    const actionMap = { board_hit: '打板介入', half_way: '半路关注', low_buy: '低吸布局', wait: '观望' };
    const strategy = strategyMap[plan.buy_strategy] || plan.buy_strategy || actionMap[plan.action] || plan.action_name || plan.action || '待确认';
    const pos = plan.position_pct != null ? (plan.position_pct * 100).toFixed(0) + '%' : '--';
    const priceLow = Number(plan.buy_price_low || 0);
    const priceHigh = Number(plan.buy_price_high || 0);
    const buy = priceLow || priceHigh ? `${priceLow ? priceLow.toFixed(2) : '--'} ~ ${priceHigh ? priceHigh.toFixed(2) : '--'}` : '--';
    const stop = plan.stop_loss_price ? Number(plan.stop_loss_price).toFixed(2) : (plan.stop_loss || '--');
    const rr = plan.risk_reward_ratio != null ? Number(plan.risk_reward_ratio).toFixed(1) : '--';
    const exp = plan.expected_return != null ? Number(plan.expected_return).toFixed(1) + '%' : '--';
    const grade = plan.certainty_level || plan.certainty_grade || '';
    const bayes = plan.bayes_probability != null ? (Number(plan.bayes_probability) * 100).toFixed(0) + '%' : '';

    const buyConditions = details.buy_conditions || details.conditions || [];
    const stopRules = details.stop_rules || details.exit_rules || details.risk_rules || [];
    const takeProfitRules = details.take_profit_rules || details.profit_rules || [];
    const managementRules = details.management_rules || [];
    const scenarios = details.scenarios;
    let scenarioList = [];
    if (Array.isArray(scenarios)) {
        scenarioList = scenarios;
    } else if (scenarios && typeof scenarios === 'object') {
        scenarioList = Object.entries(scenarios).map(([k, v]) => {
            const nameMap = { best: '最好', neutral: '基准', worst: '最坏' };
            if (v && typeof v === 'object') {
                return { scenario: nameMap[k] || k, desc: v.scenario || '', action: v.return_pct != null ? `收益${Number(v.return_pct).toFixed(1)}%` : '' };
            }
            return { scenario: nameMap[k] || k, desc: String(v), action: '' };
        });
    }
    const signals = Array.isArray(plan.signals) ? plan.signals.slice(0, 2) : [];
    const risks = Array.isArray(plan.risks) ? plan.risks.slice(0, 2) : [];
    const target = plan.take_profit_price ? Number(plan.take_profit_price).toFixed(2) :
        (takeProfitRules[0] && /\d+\.\d+/.test(takeProfitRules[0]) ? takeProfitRules[0].match(/\d+\.\d+/)[0] : '--');

    return `
    <div class="plan-panel">
        <div class="plan-panel-title"><i class="bi bi-clipboard-check"></i> 操作计划 ${grade ? `<span>${grade}</span>` : ''}</div>
        <div class="row g-2 plan-stat-grid">
            <div><span>策略</span><b>${strategy}</b></div>
            <div><span>仓位</span><b class="text-gold">${pos}</b></div>
            <div><span>买点</span><b>${buy}</b></div>
            <div><span>止损</span><b class="text-danger">${stop}</b></div>
            <div><span>止盈</span><b class="text-success">${target}</b></div>
            <div><span>盈亏比</span><b>${rr}</b></div>
            <div><span>期望收益</span><b>${exp}</b></div>
            ${bayes ? `<div><span>次日概率</span><b>${bayes}</b></div>` : ''}
        </div>
        ${buyConditions.length ? `<div class="plan-line plan-buy"><b>介入条件：</b>${buyConditions.slice(0, 3).join('；')}</div>` : ''}
        ${signals.length ? `<div class="plan-line"><b>确认信号：</b>${signals.join('；')}</div>` : ''}
        ${scenarioList.length ? `<div class="plan-scenarios">${scenarioList.slice(0, 3).map(s => typeof s === 'string' ? `<span>${s}</span>` : `<span>${s.scenario || s.name || ''}：${s.desc || s.action || ''}</span>`).join('')}</div>` : ''}
        ${takeProfitRules.length ? `<div class="plan-line"><b>止盈规则：</b>${takeProfitRules.slice(0, 2).join('；')}</div>` : ''}
        ${managementRules.length ? `<div class="plan-line"><b>持仓管理：</b>${managementRules.slice(0, 2).join('；')}</div>` : ''}
        ${stopRules.length ? `<div class="plan-line plan-stop"><b>离场规则：</b>${stopRules.slice(0, 2).join('；')}</div>` : ''}
        ${risks.length ? `<div class="plan-line plan-stop"><b>风险提示：</b>${risks.join('；')}</div>` : ''}
    </div>`;
}

function renderRecEntryMini(items) {
    const box = document.getElementById('rec-entry-mini');
    if (!box) return;
    if (!items || items.length === 0) {
        box.querySelector('.card-body').innerHTML = '<div class="text-muted small p-2">暂无进场确定性数据</div>';
        return;
    }
    const gradeColor = {
        'S+': 'linear-gradient(135deg,#ff4d4f,#faad14)', 'S': 'linear-gradient(135deg,#faad14,#ffd666)',
        'A': 'linear-gradient(135deg,#1890ff,#69c0ff)', 'B': 'linear-gradient(135deg,#52c41a,#95de64)',
        'C': 'linear-gradient(135deg,#8c8c8c,#bfbfbf)', 'D': 'linear-gradient(135deg,#595959,#8c8c8c)',
    };
    box.querySelector('.card-body').innerHTML = items.slice(0, 8).map(r => {
        const gc = gradeColor[r.certainty_grade] || gradeColor.C;
        const pos = r.position_pct > 0 ? (r.position_pct * 100).toFixed(0) + '%' : '0%';
        const bp = r.bayes_probability != null ? (r.bayes_probability * 100).toFixed(0) + '%' : '--';
        return `<div class="entry-mini-row">
            <div><span class="entry-mini-grade" style="background:${gc}">${r.certainty_grade || '--'}</span><b>${r.name || ''}</b><small>${r.boards || ''}板</small></div>
            <div><b class="text-gold">${Number(r.composite_score || 0).toFixed(0)}</b><small>次日${bp}</small><small>仓${pos}</small></div>
        </div>`;
    }).join('');
}

function renderRadarChart(recs) {
    const ctx = document.getElementById('radarChart');
    if (!ctx) return;
    if (radarChartInstance) radarChartInstance.destroy();

    const dims = ['concept_heat', 'board_position', 'seal_quality', 'cap_fit', 'volume_price'];
    const labels = dims.map(d => dimLabels[d] || d);
    const colors = ['#f0b90b', '#22d3ee', '#0ecb81', '#a855f7', '#f6465d', '#1e90ff', '#fcd535', '#4ecdc4'];
    const topRecs = (recs || []).slice().sort((a, b) => Number(b.total_score || 0) - Number(a.total_score || 0)).slice(0, 5);

    if (!topRecs.length) {
        radarChartInstance = new Chart(ctx, {
            type: 'radar',
            data: { labels, datasets: [{ label: '暂无推荐', data: [0, 0, 0, 0, 0], borderColor: '#4b5563', backgroundColor: 'rgba(75,85,99,0.1)' }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#9ca3af' } } }, scales: { r: { min: 0, max: 100, ticks: { color: '#9ca3af', backdropColor: 'transparent', stepSize: 25 }, grid: { color: 'rgba(45,55,72,0.45)' }, pointLabels: { color: '#e5e7eb', font: { size: 11 } }, angleLines: { color: 'rgba(45,55,72,0.45)' } } } }
        });
        return;
    }

    const datasets = topRecs.map((r, idx) => {
        const ds = normalizeDimScores(r.dimension_scores);
        const color = colors[idx % colors.length];
        return {
            label: r.name || `标的${idx + 1}`,
            data: dims.map(d => Number(ds[d] || 0)),
            borderColor: color,
            backgroundColor: color + '22',
            borderWidth: 2,
            pointRadius: 3,
            pointBackgroundColor: color,
        };
    });

    radarChartInstance = new Chart(ctx, {
        type: 'radar',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#9ca3af', boxWidth: 10, font: { size: 10 } } } },
            scales: {
                r: {
                    min: 0,
                    max: 100,
                    ticks: { color: '#9ca3af', backdropColor: 'transparent', stepSize: 25 },
                    grid: { color: 'rgba(45,55,72,0.5)' },
                    pointLabels: { color: '#e5e7eb', font: { size: 11 } },
                    angleLines: { color: 'rgba(45,55,72,0.5)' },
                }
            }
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
    
    const actionConfig = {
        'CLEAR_ALL': { icon: '🔴', text: '清仓观望', bg: 'rgba(239,83,80,0.12)', border: 'rgba(239,83,80,0.4)', color: '#ef5350' },
        'REDUCE':    { icon: '🟠', text: '减仓防守', bg: 'rgba(255,152,0,0.12)', border: 'rgba(255,152,0,0.4)', color: '#ff9800' },
        'HOLD':      { icon: '🟡', text: '保持仓位，不出新仓', bg: 'rgba(255,235,59,0.12)', border: 'rgba(255,235,59,0.4)', color: '#ffeb3b' },
        'NORMAL':    { icon: '🟢', text: '正常参与', bg: 'rgba(14,203,129,0.12)', border: 'rgba(14,203,129,0.4)', color: '#0ecb81' }
    };
    const ac = actionConfig[action] || actionConfig['NORMAL'];
    
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
    const cumWR = tracking.cumulative_win_rate || 0;
    document.getElementById('track-winrate').textContent = formatPercent(cumWR);
    document.getElementById('track-total-rec').textContent = result.cumulative?.total_recommendations || tracking.recommendations_count || '--';
    document.getElementById('track-total-correct').textContent = result.cumulative?.total_correct || tracking.correct_count || '--';
    document.getElementById('track-daily-winrate').textContent = formatPercent(tracking.win_rate);
    const details = tracking.details || [];
    const tbody = document.getElementById('track-detail-body');
    if (details.length === 0) { tbody.innerHTML = '<tr><td colspan="6" class="text-muted">当日无推荐记录</td></tr>'; }
    else {
        tbody.innerHTML = details.map(d => {
            const mark = d.is_correct ? '✅ 命中' : '❌ 未命中';
            return `<tr><td>${d.code}</td><td>${d.name}</td><td>${formatNumber(d.score, 1)}</td><td>${d.suggested_action || '--'}</td><td>${d.result_desc || '--'}</td><td>${mark}</td></tr>`;
        }).join('');
    }
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
    document.getElementById('upg-total-rec').textContent = accuracy.total_recommendations || '--';
    document.getElementById('upg-winrate').textContent = formatPercent(accuracy.overall_win_rate);
    const period = accuracy.period || [];
    document.getElementById('upg-period').textContent = period.length >= 2 ? `${period[0]} ~ ${period[1]}` : '--';
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
    renderWeightCompare(oldW, newW);
    const regimeHtml = `
        <div class="row g-3">
            <div class="col-md-4 text-center"><div class="card-label">当前风格</div><div class="regime-badge current" style="margin-top:0.5rem">${regime.current_regime || '--'}</div></div>
            <div class="col-md-4 text-center"><div class="card-label">上一风格</div><div style="margin-top:0.5rem;font-size:1rem;color:var(--text-muted)">${regime.prev_regime || '--'}</div></div>
            <div class="col-md-4 text-center"><div class="card-label">状态</div><div style="margin-top:0.5rem;font-size:1.2rem">${regime.is_changed ? '<span class="regime-badge changed">⚠️ 风格切换</span>' : '<span style="color:var(--green)">✅ 稳定</span>'}</div></div>
        </div>
        ${(regime.evidence || []).length > 0 ? `<div style="margin-top:1rem"><div class="strategy-label">检测依据</div>${regime.evidence.map(e => `<div class="strategy-text">• ${e}</div>`).join('')}</div>` : ''}
    `;
    document.getElementById('upg-regime-display').innerHTML = regimeHtml;
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

    renderNetChart(result.net_values);

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

// ============ 量化策略 ============
let quantConfigVisible = false;

function toggleQuantConfig() {
    quantConfigVisible = !quantConfigVisible;
    document.getElementById('quant-config').style.display = quantConfigVisible ? 'block' : 'none';
}

async function runQuantSignals() {
    const btn = document.getElementById('btn-quant-signals');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 生成中...';
    try {
        const resp = await fetchJSON('/api/quant/signals');
        if (!resp.success) throw new Error(resp.error || '获取信号失败');
        showQuantSignals(resp.data);
    } catch (e) {
        alert('生成信号失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-fill"></i> 生成今日信号';
    }
}

function showQuantSignals(data) {
    document.getElementById('quant-results').style.display = 'block';
    const market = data.market || {};
    document.getElementById('quant-market-state').innerHTML = `
        <div class="col-md-3"><div class="card card-dark"><div class="card-body text-center">
            <div class="card-label">周期阶段</div>
            <div class="card-value" style="font-size:1.2rem;color:${market.cycle_phase === '爆发高潮期' ? 'var(--gold)' : market.cycle_phase === '崩塌退潮期' ? 'var(--red)' : 'var(--text-primary)'}">${market.cycle_phase || '--'}</div>
        </div></div></div>
        <div class="col-md-3"><div class="card card-dark"><div class="card-body text-center">
            <div class="card-label">砸盘系数</div>
            <div class="card-value ${getSmashColor(market.smash_coefficient)}">${formatNumber(market.smash_coefficient)}</div>
        </div></div></div>
        <div class="col-md-3"><div class="card card-dark"><div class="card-body text-center">
            <div class="card-label">炸板率</div>
            <div class="card-value">${formatPercent(market.explosion_rate)}</div>
        </div></div></div>
        <div class="col-md-3"><div class="card card-dark"><div class="card-body text-center">
            <div class="card-label">涨停数</div>
            <div class="card-value">${market.limit_up_count}</div>
        </div></div></div>
    `;

    const buySignals = data.buy_signals || [];
    const buyContainer = document.getElementById('quant-buy-signals');
    if (buySignals.length === 0) {
        buyContainer.innerHTML = '<div style="color:#9ca3af;padding:20px;text-align:center">当前无符合条件的买入信号</div>';
    } else {
        buyContainer.innerHTML = buySignals.map(s => `
            <div class="rec-stock-card" style="border-left-color:${s.grade === 'S' ? 'var(--gold)' : 'var(--green)'}">
                <div class="rec-stock-header">
                    <div>
                        <span class="rec-stock-name">${s.name}</span>
                        <span class="rec-stock-code">${s.code}</span>
                        <span class="rec-stock-tag" style="background:${s.grade === 'S' ? 'rgba(240,185,11,0.2)' : 'rgba(14,203,129,0.2)'};color:${s.grade === 'S' ? 'var(--gold)' : 'var(--green)'}">${s.grade}级</span>
                    </div>
                    <div class="rec-stock-score">${s.position_pct.toFixed(0)}%</div>
                </div>
                <div class="rec-stock-meta">
                    <span class="rec-stock-tag">价格 ${s.price.toFixed(2)}</span>
                    <span class="rec-stock-tag action">置信度 ${(s.confidence * 100).toFixed(0)}%</span>
                </div>
                <div class="rec-stock-reason">✅ ${s.reasons.join('；')}</div>
                ${s.risk_warnings && s.risk_warnings.length > 0 ? `<div class="rec-stock-risks">⚠️ ${s.risk_warnings.join('；')}</div>` : ''}
            </div>
        `).join('');
    }

    const sellSignals = data.sell_signals || [];
    const sellContainer = document.getElementById('quant-sell-signals');
    if (sellSignals.length === 0) {
        sellContainer.innerHTML = '<div style="color:#9ca3af;padding:20px;text-align:center">当前无卖出信号</div>';
    } else {
        sellContainer.innerHTML = sellSignals.map(s => `
            <div class="rec-stock-card" style="border-left-color:var(--red)">
                <div class="rec-stock-header">
                    <div>
                        <span class="rec-stock-name" style="color:var(--red)">${s.name}</span>
                        <span class="rec-stock-code">${s.code}</span>
                        <span class="rec-stock-tag" style="background:rgba(246,70,93,0.2);color:var(--red)">${s.grade}级</span>
                    </div>
                    <div class="rec-stock-score" style="color:var(--red)">${s.price.toFixed(2)}</div>
                </div>
                <div class="rec-stock-reason" style="color:#f6465d">🔴 ${s.reasons.join('；')}</div>
            </div>
        `).join('');
    }
}

async function runQuantBacktest() {
    const btn = document.querySelector('#tab-quant .btn-outline-light');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 回测中...';
    const cash = (parseFloat(document.getElementById('q-backtest-cash').value) || 100) * 10000;
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - 90);
    try {
        const resp = await postJSON('/api/quant/backtest', {
            start_date: start.toISOString().split('T')[0],
            end_date: end.toISOString().split('T')[0],
            init_cash: cash
        });
        if (!resp.success) throw new Error(resp.error || '回测失败');
        showQuantBacktest(resp.data);
    } catch (e) {
        alert('回测失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-clock-history"></i> 运行回测';
    }
}

function showQuantBacktest(data) {
    const container = document.getElementById('quant-backtest-results');
    const tr = data.total_return || 0;
    const dd = data.max_drawdown || 0;
    const wr = data.win_rate || 0;
    container.innerHTML = `
        <div class="row g-2">
            <div class="col-6 col-md-3"><div class="card-label">累计收益</div><div class="card-value" style="color:${tr >= 0 ? 'var(--green)' : 'var(--red)'}">${(tr * 100).toFixed(2)}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">最大回撤</div><div class="card-value" style="color:var(--red)">${(dd * 100).toFixed(2)}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">胜率</div><div class="card-value">${(wr * 100).toFixed(1)}%</div></div>
            <div class="col-6 col-md-3"><div class="card-label">交易次数</div><div class="card-value">${data.total_trades || 0}</div></div>
        </div>
        <div style="margin-top:10px;font-size:0.8rem;color:#9ca3af">回测区间 ${data.start_date} ~ ${data.end_date}</div>
    `;
}

// ============ 初始化 ============
document.addEventListener('DOMContentLoaded', () => {
    loadDashboard();
    document.getElementById('btn-run-simulate').addEventListener('click', runSimulate);
});