/**
 * 首页轮播控制器 (Vanilla JS)
 * 特性：自动播放 | 视频等待播完 | 懒加载 | 左右箭头 | 指示点
 */

(function () {
    'use strict';

    /** 配置 */
    const IMAGE_INTERVAL = 5000; // 图片切换间隔（毫秒）

    /** DOM 引用 */
    const slider = document.querySelector('.hero-slider');
    if (!slider) return; // 没有轮播容器则退出

    const slides = Array.from(slider.querySelectorAll('.slide'));
    const dotsContainer = slider.querySelector('.slider-dots');
    if (!slides.length) return;

    let current = 0;
    let timer = null;
    let pendingFromVideo = false; // 是否正等待视频结束

    // ==================== 懒加载 ====================
    function loadSlide(index) {
        const slide = slides[index];
        if (!slide || slide.dataset.loaded) return;
        const img = slide.querySelector('img');
        const video = slide.querySelector('video');
        if (img && img.dataset.src) {
            img.src = img.dataset.src;
            delete img.dataset.src;
        }
        if (video && video.dataset.src) {
            const source = video.querySelector('source');
            if (source && source.dataset.src) {
                source.src = source.dataset.src;
                delete source.dataset.src;
                video.load();
            }
        }
        slide.dataset.loaded = '1';
    }

    // ==================== 构建指示点 ====================
    function buildDots() {
        dotsContainer.innerHTML = '';
        slides.forEach(function (_, i) {
            var btn = document.createElement('button');
            btn.className = 'slider-dot' + (i === 0 ? ' active' : '');
            btn.setAttribute('aria-label', '切换到第 ' + (i + 1) + ' 张');
            btn.addEventListener('click', function () { goTo(i); });
            dotsContainer.appendChild(btn);
        });
    }
    buildDots();

    function updateDots() {
        var dots = dotsContainer.querySelectorAll('.slider-dot');
        dots.forEach(function (d, i) { d.classList.toggle('active', i === current); });
    }

    // ==================== 切换逻辑 ====================
    function goTo(index) {
        if (index === current) return;
        slides[current].classList.remove('active');
        current = ((index % slides.length) + slides.length) % slides.length;
        slides[current].classList.add('active');
        updateDots();
        // 预加载下一张
        loadSlide((current + 1) % slides.length);
        scheduleNext();
    }

    function next() { goTo(current + 1); }
    function prev() { goTo(current - 1); }

    // ==================== 自动播放 ====================
    function clearTimer() {
        if (timer) { clearTimeout(timer); timer = null; }
    }

    function scheduleNext() {
        clearTimer();
        if (pendingFromVideo) return;

        var activeSlide = slides[current];
        var video = activeSlide ? activeSlide.querySelector('video') : null;

        if (video) {
            // 视频：等待 ended 事件
            video.currentTime = 0;
            video.play().catch(function () {});
            pendingFromVideo = true;
            video.onended = function () {
                pendingFromVideo = false;
                next();
            };
            // 兜底：60 秒后自动跳
            timer = setTimeout(function () {
                if (pendingFromVideo) {
                    pendingFromVideo = false;
                    next();
                }
            }, 60000);
        } else {
            // 图片：定时切换
            timer = setTimeout(next, IMAGE_INTERVAL);
        }
    }

    // 暂停视频（离开时）
    function stopCurrentVideo() {
        var activeSlide = slides[current];
        if (!activeSlide) return;
        var video = activeSlide.querySelector('video');
        if (video) {
            video.pause();
            video.onended = null;
        }
        pendingFromVideo = false;
    }

    // ==================== 箭头 ====================
    function buildArrows() {
        var prevBtn = document.createElement('button');
        prevBtn.className = 'slider-arrow prev';
        prevBtn.innerHTML = '<i class="fas fa-chevron-left"></i>';
        prevBtn.setAttribute('aria-label', '上一张');
        prevBtn.addEventListener('click', function () {
            stopCurrentVideo();
            clearTimer();
            prev();
        });

        var nextBtn = document.createElement('button');
        nextBtn.className = 'slider-arrow next';
        nextBtn.innerHTML = '<i class="fas fa-chevron-right"></i>';
        nextBtn.setAttribute('aria-label', '下一张');
        nextBtn.addEventListener('click', function () {
            stopCurrentVideo();
            clearTimer();
            next();
        });

        slider.appendChild(prevBtn);
        slider.appendChild(nextBtn);
    }
    buildArrows();

    // ==================== 触摸支持 ====================
    var touchStartX = 0;
    slider.addEventListener('touchstart', function (e) {
        touchStartX = e.touches[0].clientX;
    }, { passive: true });
    slider.addEventListener('touchend', function (e) {
        var diff = touchStartX - e.changedTouches[0].clientX;
        if (Math.abs(diff) > 50) {
            stopCurrentVideo();
            clearTimer();
            diff > 0 ? next() : prev();
        }
    });

    // ==================== 键盘支持 ====================
    document.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowLeft') { stopCurrentVideo(); clearTimer(); prev(); }
        if (e.key === 'ArrowRight') { stopCurrentVideo(); clearTimer(); next(); }
    });

    // ==================== 启动 ====================
    // 预加载首张 + 下张
    loadSlide(0);
    loadSlide(1);
    scheduleNext();
})();
