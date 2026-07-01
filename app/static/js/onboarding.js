/**
 * 冷水坑杯 #3 新手引导系统 V2
 *
 * 架构：
 *   - 数据驱动：所有步骤在 STEPS 数组中配置
 *   - 真实挖空遮罩：SVG mask 实现目标区域完全透明、可点击
 *   - 跨页面导航：SessionStorage 持久化状态，每步自动切换页面
 *   - 页面锁定：教程期间禁止导航栏点击
 *   - 窗口自适应：resize + scroll 实时重定位
 *
 * 引导流程（8步跨7个页面）：
 *   欢迎 → 首页 → 队伍列表 → 赛事规则 → 赛事大厅 → 排行榜 → 个人中心 → 完成
 */
(function () {
    'use strict';

    // ========== 步骤配置（数据驱动） ==========
    const STEPS = [
        {
            id: 'welcome',
            type: 'welcome',
            title: '欢迎来到冷水坑杯 #3',
            icon: 'fa-crown',
            content: '这是一场明日方舟肉鸽主题的团队比赛。<br>本教程将用 <strong>约 2 分钟</strong> 带您快速了解参赛流程。',
            url: null,  // 不跳转，在当前页弹窗
            target: null,
            position: null,
        },
        {
            id: 'home',
            type: 'highlight',
            title: '首页',
            icon: 'fa-home',
            content: '这里展示赛事公告、轮播图、当前赛事和最新动态，是了解整个赛事的第一站。',
            reason: '快速了解赛事概况',
            url: '/',
            target: '.carousel, .card:first-of-type',
            position: 'bottom',
        },
        {
            id: 'teams',
            type: 'highlight',
            title: '队伍列表',
            icon: 'fa-users',
            content: '查看所有参赛队伍，点击队伍可查看成员、宣言和队伍照片。',
            reason: '了解对手，找到心仪的队伍',
            url: '/teams',
            target: '.card, .table',
            position: 'right',
        },
        {
            id: 'rules',
            type: 'highlight',
            title: '赛事规则',
            icon: 'fa-book',
            content: '详细介绍比赛流程、积分规则和注意事项。<strong>建议参赛前仔细阅读。</strong>',
            reason: '不读规则可能会吃亏',
            url: '/rules',
            target: '.card .card-body, .container > .card, main .container',
            position: 'right',
        },
        {
            id: 'tournament',
            type: 'highlight',
            title: '赛事大厅',
            icon: 'fa-trophy',
            content: '比赛的核心区域。在这里可以查看比赛安排、实时状态、排行榜和比赛动态。',
            reason: '比赛相关的一切都在这里',
            url: '/tournament',
            target: '.card, .table',
            position: 'right',
        },
        {
            id: 'rankings',
            type: 'highlight',
            title: '排行榜',
            icon: 'fa-list-ol',
            content: '查看队伍排名、个人排名和积分排行，实时更新。看看谁在领跑！',
            reason: '一览赛场风云',
            url: '/tournament/rankings',
            target: '.card, .table',
            position: 'right',
        },
        {
            id: 'profile',
            type: 'highlight',
            title: '个人中心',
            icon: 'fa-user-circle',
            content: '在这里管理头像、昵称、参赛宣言和个人资料。完善资料后等待队长邀请即可。',
            reason: '你的参赛入口',
            url: '/player/profile',
            target: '.card:first-of-type',
            position: 'right',
        },
        {
            id: 'complete',
            type: 'complete',
            title: '准备就绪！',
            icon: 'fa-check-circle',
            content: '你已经了解了冷水坑杯的基本流程。<br>现在就去找队伍、选肉鸽、打比赛吧！',
            url: null,
            target: null,
            position: null,
            nextSteps: [
                '前往「个人中心」完善个人资料',
                '等待或主动寻找队伍加入',
                '选择你的肉鸽主题并填写宣言',
                '在「赛事大厅」查看排行榜和比赛',
                '随时点击右下角 <i class="fas fa-question"></i> 按钮重新查看引导',
            ],
        },
    ];

    const STORAGE_KEY = 'lskb_onboarding';

    // ========== 状态管理（从 SessionStorage 恢复） ==========
    let state = {
        active: false,
        stepIndex: 0,
        justNavigated: false,  // 刚完成页面跳转，等待渲染
    };

    function loadState() {
        try {
            const raw = sessionStorage.getItem(STORAGE_KEY);
            if (raw) {
                const saved = JSON.parse(raw);
                if (saved.active) {
                    state.active = true;
                    state.stepIndex = saved.stepIndex || 0;
                    state.justNavigated = true;
                }
            }
        } catch (e) { /* ignore */ }
    }

    function saveState() {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
            active: state.active,
            stepIndex: state.stepIndex,
        }));
    }

    function clearState() {
        sessionStorage.removeItem(STORAGE_KEY);
    }

    // ========== DOM 元素引用 ==========
    let svgMask = null;       // SVG mask 元素（全局共享）
    let overlayEl = null;     // 遮罩层
    let tooltipEl = null;     // 提示气泡/弹窗
    let navOverlayEl = null;  // 导航栏锁定遮罩
    let helpBtnEl = null;     // 帮助按钮引用

    // ========== DOM 工具 ==========
    function createEl(tag, className, innerHTML) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (innerHTML) el.innerHTML = innerHTML;
        return el;
    }

    function $(selector) {
        try { return document.querySelector(selector); } catch (e) { return null; }
    }

    function getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) return meta.getAttribute('content');
        const match = document.cookie.match(/csrf_token=([^;]+)/);
        return match ? match[1] : '';
    }

    // ========== SVG Mask 真实挖空遮罩 ==========
    // 原理：全屏 SVG <rect> + <mask>，在 mask 中挖出一个透明的矩形窗口
    // 窗口内部完全透明、可点击（pointer-events: none on SVG）

    function createSvgOverlay() {
        const svgNs = 'http://www.w3.org/2000/svg';

        // 创建 SVG 元素
        const svg = document.createElementNS(svgNs, 'svg');
        svg.setAttribute('class', 'onboarding-svg-overlay');
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.style.cssText = 'position:fixed;inset:0;z-index:9990;pointer-events:none;';

        // 创建 mask
        const mask = document.createElementNS(svgNs, 'mask');
        mask.setAttribute('id', 'onboarding-cutout-mask');

        // mask 中先画一个白色 fill（全部可见 = 全部遮罩）
        const fullRect = document.createElementNS(svgNs, 'rect');
        fullRect.setAttribute('width', '100%');
        fullRect.setAttribute('height', '100%');
        fullRect.setAttribute('fill', 'white');
        mask.appendChild(fullRect);

        // 再画一个黑色矩形（挖空区域 = 透明）
        const cutout = document.createElementNS(svgNs, 'rect');
        cutout.setAttribute('id', 'onboarding-cutout');
        cutout.setAttribute('fill', 'black');
        cutout.setAttribute('rx', '8');
        cutout.setAttribute('ry', '8');
        mask.appendChild(cutout);

        // 应用 mask 的 rect（遮罩层）
        const overlay = document.createElementNS(svgNs, 'rect');
        overlay.setAttribute('width', '100%');
        overlay.setAttribute('height', '100%');
        overlay.setAttribute('fill', 'rgba(0,0,0,0.65)');
        overlay.setAttribute('mask', 'url(#onboarding-cutout-mask)');
        overlay.style.cssText = 'pointer-events:auto;';  // 遮罩区域可拦截点击

        svg.appendChild(mask);
        svg.appendChild(overlay);
        document.body.appendChild(svg);

        svgMask = {
            svg: svg,
            cutout: cutout,
            overlay: overlay,
        };
    }

    function updateCutout(targetEl) {
        if (!svgMask || !targetEl) return;
        const rect = targetEl.getBoundingClientRect();
        const pad = 8;
        svgMask.cutout.setAttribute('x', Math.max(0, rect.left - pad));
        svgMask.cutout.setAttribute('y', Math.max(0, rect.top - pad));
        svgMask.cutout.setAttribute('width', rect.width + pad * 2);
        svgMask.cutout.setAttribute('height', rect.height + pad * 2);
    }

    function removeSvgOverlay() {
        if (svgMask) {
            svgMask.svg.remove();
            svgMask = null;
        }
    }

    // ========== 导航栏锁定 ==========
    // 教程期间禁用导航栏点击（pointer-events: none），而非全屏覆盖层
    // 这样 tooltip 可以正常工作，不需要处理 z-index 冲突
    function lockNavbar() {
        const navbar = document.querySelector('.navbar');
        if (navbar) {
            navbar.style.pointerEvents = 'none';
            navbar.style.opacity = '0.6';
        }
        // 同时禁用页脚
        const footer = document.querySelector('.footer');
        if (footer) {
            footer.style.pointerEvents = 'none';
            footer.style.opacity = '0.6';
        }
    }

    function unlockNavbar() {
        const navbar = document.querySelector('.navbar');
        if (navbar) {
            navbar.style.pointerEvents = '';
            navbar.style.opacity = '';
        }
        const footer = document.querySelector('.footer');
        if (footer) {
            footer.style.pointerEvents = '';
            footer.style.opacity = '';
        }
    }

    // ========== 渲染：欢迎弹窗 ==========
    function renderWelcome(step) {
        const el = createEl('div', 'onboarding-welcome',
            `<div class="welcome-icon"><i class="fas ${step.icon}"></i></div>
             <h3>${step.title}</h3>
             <p>${step.content}</p>
             <div class="onboarding-buttons" style="justify-content:center;">
                 <button class="btn btn-onboarding-primary" data-action="next">
                     <i class="fas fa-play"></i> 开始引导
                 </button>
                 <button class="btn btn-onboarding-skip" data-action="skip">
                     跳过引导
                 </button>
             </div>`
        );
        return el;
    }

    // ========== 渲染：完成弹窗 ==========
    function renderComplete(step) {
        let nextStepsHtml = '';
        if (step.nextSteps && step.nextSteps.length) {
            nextStepsHtml = '<div class="next-steps"><ul>' +
                step.nextSteps.map(s => `<li><i class="fas fa-chevron-right"></i> ${s}</li>`).join('') +
                '</ul></div>';
        }
        const el = createEl('div', 'onboarding-complete',
            `<div class="complete-icon">🎉</div>
             <h3><i class="fas ${step.icon}"></i> ${step.title}</h3>
             <p>${step.content}</p>
             ${nextStepsHtml}
             <div class="onboarding-buttons" style="justify-content:center;">
                 <button class="btn btn-onboarding-primary" data-action="finish">
                     <i class="fas fa-check"></i> 开始使用
                 </button>
                 <button class="btn btn-onboarding-secondary" data-action="restart">
                     <i class="fas fa-redo"></i> 重新观看
                 </button>
             </div>`
        );
        return el;
    }

    // ========== 渲染：提示气泡 ==========
    function renderTooltip(step) {
        const highlightSteps = STEPS.filter(s => s.type === 'highlight');
        const totalHL = highlightSteps.length;
        const hlIndex = highlightSteps.findIndex(s => s.id === step.id);

        // 进度圆点
        const dotsHtml = highlightSteps.map((_, i) => {
            let cls = 'progress-dot';
            if (i < hlIndex) cls += ' done';
            if (i === hlIndex) cls += ' active';
            return `<span class="${cls}"></span>`;
        }).join('');

        const reasonHtml = step.reason
            ? `<div class="onboarding-reason"><i class="fas fa-lightbulb"></i> ${step.reason}</div>`
            : '';

        const el = createEl('div', 'onboarding-tooltip',
            `<div class="onboarding-progress">${dotsHtml}</div>
             <h5><span class="step-number">${hlIndex + 1}/${totalHL}</span> ${step.title}</h5>
             <p>${step.content}</p>
             ${reasonHtml}
             <div class="onboarding-buttons">
                 ${hlIndex > 0
                     ? '<button class="btn btn-onboarding-secondary" data-action="prev"><i class="fas fa-arrow-left"></i> 上一步</button>'
                     : ''}
                 <button class="btn btn-onboarding-primary" data-action="next">
                     ${hlIndex < totalHL - 1 ? '下一步 <i class="fas fa-arrow-right"></i>' : '完成 <i class="fas fa-check"></i>'}
                 </button>
                 <button class="btn btn-onboarding-skip" data-action="skip">跳过</button>
             </div>`
        );
        return el;
    }

    // ========== 定位 tooltip ==========
    function positionTooltip(tooltip, targetEl, preferredPos) {
        const targetRect = targetEl.getBoundingClientRect();
        const tipRect = tooltip.getBoundingClientRect();
        const margin = 16;
        const winW = window.innerWidth;
        const winH = window.innerHeight;

        // 尝试位置优先级：传入 → right → bottom → left → top
        const order = [preferredPos, 'right', 'bottom', 'left', 'top'];
        // 去重
        const positions = [...new Set(order)];

        let best = null;

        for (const pos of positions) {
            let top, left;
            switch (pos) {
                case 'right':
                    top = targetRect.top + (targetRect.height - tipRect.height) / 2;
                    left = targetRect.right + margin;
                    break;
                case 'bottom':
                    top = targetRect.bottom + margin;
                    left = targetRect.left + (targetRect.width - tipRect.width) / 2;
                    break;
                case 'left':
                    top = targetRect.top + (targetRect.height - tipRect.height) / 2;
                    left = targetRect.left - tipRect.width - margin;
                    break;
                case 'top':
                    top = targetRect.top - tipRect.height - margin;
                    left = targetRect.left + (targetRect.width - tipRect.width) / 2;
                    break;
                default:
                    continue;
            }

            // 边界约束
            const pad = 10;
            const clampedLeft = Math.max(pad, Math.min(left, winW - tipRect.width - pad));
            const clampedTop = Math.max(pad, Math.min(top, winH - tipRect.height - pad));

            // 检查是否超出边界太多（超过一半被裁剪就算不合适）
            const overflowX = (clampedLeft !== left) ? Math.abs(left - clampedLeft) : 0;
            const overflowY = (clampedTop !== top) ? Math.abs(top - clampedTop) : 0;

            if (overflowX < tipRect.width * 0.4 && overflowY < tipRect.height * 0.4) {
                best = { top: clampedTop, left: clampedLeft };
                break;
            }
            if (!best) {
                best = { top: clampedTop, left: clampedLeft };
            }
        }

        tooltip.style.top = best.top + 'px';
        tooltip.style.left = best.left + 'px';
    }

    // ========== 清理 UI ==========
    function cleanupUI() {
        removeSvgOverlay();
        if (tooltipEl) { tooltipEl.remove(); tooltipEl = null; }
    }

    function cleanupAll() {
        cleanupUI();
        unlockNavbar();
    }

    // ========== 渲染当前步骤 ==========
    function renderStep() {
        cleanupUI();
        const step = STEPS[state.stepIndex];
        if (!step) return;

        if (step.type === 'welcome') {
            createSvgOverlay();
            updateCutout(null);  // 全遮罩，无挖空
            tooltipEl = renderWelcome(step);
            document.body.appendChild(tooltipEl);
        } else if (step.type === 'complete') {
            createSvgOverlay();
            updateCutout(null);
            tooltipEl = renderComplete(step);
            document.body.appendChild(tooltipEl);
        } else if (step.type === 'highlight') {
            const targetEl = resolveTarget(step.target);
            if (targetEl) {
                createSvgOverlay();
                updateCutout(targetEl);

                // 滚动到目标可见
                targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });

                tooltipEl = renderTooltip(step);
                document.body.appendChild(tooltipEl);

                // 定位（等 scrollIntoView 完成）
                setTimeout(() => {
                    if (svgMask) updateCutout(targetEl);
                    if (tooltipEl) positionTooltip(tooltipEl, targetEl, step.position || 'right');
                }, 400);
            } else {
                // 目标元素不存在——跳过此步，转到下一步
                console.warn('[Onboarding] Target not found:', step.target, '— skipping step', step.id);
                state.stepIndex = state.stepIndex + 1;
                if (state.stepIndex >= STEPS.length) { finish(); return; }
                renderStep();
                return;
            }
        }

        bindButtons();
        saveState();
    }

    function resolveTarget(selector) {
        if (!selector) return null;
        // 支持多个候选（逗号分隔），返回第一个匹配的
        const candidates = selector.split(',').map(s => s.trim());
        for (const sel of candidates) {
            const el = $(sel);
            if (el) return el;
        }
        return null;
    }

    // ========== 事件绑定 ==========
    function bindButtons() {
        if (!tooltipEl) return;
        tooltipEl.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                const action = this.dataset.action;
                switch (action) {
                    case 'next': goNext(); break;
                    case 'prev': goPrev(); break;
                    case 'skip': skipAll(); break;
                    case 'finish': finish(); break;
                    case 'restart': restart(); break;
                }
            });
        });
    }

    // ========== 导航逻辑 ==========
    function goNext() {
        const step = STEPS[state.stepIndex];
        if (!step) return;

        if (step.type === 'welcome' || step.type === 'highlight') {
            const nextIndex = state.stepIndex + 1;
            if (nextIndex >= STEPS.length) { finish(); return; }

            const nextStep = STEPS[nextIndex];
            // 每个 highlight 步骤都有对应页面 URL，需要跳转
            // welcome 的下一步是首页，也需要跳转
            // complete 没有 nextStep.url，停在当前页弹窗
            if (nextStep.url) {
                navigateToPage(nextStep.url, nextIndex);
            } else {
                // complete 弹窗（无 URL），直接在当前页渲染
                state.stepIndex = nextIndex;
                renderStep();
            }
        }
    }

    function goPrev() {
        const step = STEPS[state.stepIndex];
        if (!step || step.type === 'welcome' || step.type === 'complete') return;

        const prevIndex = state.stepIndex - 1;
        if (prevIndex < 0) return;

        const prevStep = STEPS[prevIndex];
        if (prevStep.url) {
            navigateToPage(prevStep.url, prevIndex);
        } else {
            state.stepIndex = prevIndex;
            renderStep();
        }
    }

    function navigateToPage(url, targetStepIndex) {
        // 保存目标步骤
        state.stepIndex = targetStepIndex;
        state.active = true;
        state.justNavigated = true;
        saveState();

        // 跳转（页面重载后 init() 从 SessionStorage 恢复状态）
        window.location.href = url;
    }

    function skipAll() {
        cleanupAll();
        state.active = false;
        clearState();
        sessionStorage.setItem('lskb_onboarding_done', '1');
        markCompleteToServer();
    }

    function finish() {
        cleanupAll();
        state.active = false;
        clearState();
        sessionStorage.setItem('lskb_onboarding_done', '1');
        markCompleteToServer();
    }

    function restart() {
        state.stepIndex = 0;
        state.active = true;
        state.justNavigated = false;
        saveState();
        lockNavbar();
        renderStep();
    }

    // ========== 后端 API ==========
    function markCompleteToServer() {
        fetch('/player/api/onboarding/complete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken(),
            },
        }).catch(() => {});
    }

    function resetOnboardingOnServer() {
        fetch('/player/api/onboarding/reset', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken(),
            },
        }).catch(() => {});
    }

    // ========== 窗口自适应 ==========
    let resizeTimer = null;
    function onResizeOrScroll() {
        if (!state.active || !svgMask) return;
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            const step = STEPS[state.stepIndex];
            if (!step || step.type === 'welcome' || step.type === 'complete') return;
            const targetEl = resolveTarget(step.target);
            if (targetEl && svgMask) {
                updateCutout(targetEl);
                if (tooltipEl) positionTooltip(tooltipEl, targetEl, step.position || 'right');
            }
        }, 100);
    }

    // ========== 帮助按钮 ==========
    function createHelpButton() {
        // 避免重复创建
        if (document.querySelector('.onboarding-help-btn')) return;

        const btn = createEl('button', 'onboarding-help-btn',
            '<i class="fas fa-question"></i>');
        btn.title = '查看新手引导';
        btn.addEventListener('click', () => {
            if (state.active) return;  // 已经在教程中
            resetOnboardingOnServer();
            state.active = true;
            state.stepIndex = 0;
            state.justNavigated = false;
            saveState();
            renderStep();
            lockNavbar();
        });
        document.body.appendChild(btn);
    }

    // ========== 重看按钮（个人中心） ==========
    function wireReplayButton() {
        const btn = document.getElementById('onboarding-replay-btn');
        if (btn) {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                if (state.active) return;
                resetOnboardingOnServer();
                state.active = true;
                state.stepIndex = 0;
                state.justNavigated = false;
                saveState();
                renderStep();
                lockNavbar();
            });
        }
    }

    // ========== 页面加载完成后的入口 ==========
    function init() {
        loadState();

        // 始终创建帮助按钮
        createHelpButton();

        // 连接个人中心的"重新观看"按钮
        wireReplayButton();

        // 如果是从跨页面导航过来的（或页面刷新），从 SessionStorage 恢复教程
        if (state.active) {
            state.justNavigated = false;
            saveState();
            lockNavbar();
            renderStep();
            return;
        }

        // 检查是否应该自动开始（首次登录 + 服务端注入变量）
        if (typeof SHOW_ONBOARDING === 'undefined' || !SHOW_ONBOARDING) {
            return;
        }

        // 检查 SessionStorage（防止同会话重复弹出）
        if (sessionStorage.getItem('lskb_onboarding_done') === '1') {
            return;
        }

        // 自动开始
        state.active = true;
        state.stepIndex = 0;
        state.justNavigated = false;
        saveState();
        lockNavbar();
        renderStep();
    }

    // ========== 全局事件 ==========
    window.addEventListener('resize', onResizeOrScroll);
    window.addEventListener('scroll', onResizeOrScroll, { passive: true });

    // DOM ready 后初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();