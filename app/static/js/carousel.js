/* ============================================================
   轮播 JavaScript - 宣传片风格
   ============================================================ */

(function () {
    'use strict';

    // ---- 状态 ----
    let slides = [];
    let currentIndex = 0;
    let timer = null;
    let isTransitioning = false;
    const IMAGE_DURATION = 5000;
    const TRANSITION_DURATION = 1000;

    // ---- DOM 引用 ----
    const slider = document.querySelector('.hero-slider');
    if (!slider) return;

    const track = slider.querySelector('.slider-track');
    const dotsContainer = slider.querySelector('.slider-dots');

    // ---- 初始化 ----
    function init() {
        slides = track ? Array.from(track.querySelectorAll('.slide')) : [];
        if (slides.length === 0) return;

        slides.forEach(function (s, i) {
            s.classList.toggle('active', i === 0);
        });
        currentIndex = 0;

        createDots();
        bindEvents();
        startAutoPlay();
        preloadNext();
    }

    // ---- 预加载下一张 ----
    function preloadNext() {
        if (slides.length < 2) return;
        var nextIdx = (currentIndex + 1) % slides.length;
        var nextSlide = slides[nextIdx];
        var img = nextSlide.querySelector('img');
        if (img && !img.complete) {
            var link = document.createElement('link');
            link.rel = 'preload';
            link.as = 'image';
            link.href = img.src;
            document.head.appendChild(link);
        }
    }

    // ---- 创建圆点 ----
    function createDots() {
        if (!dotsContainer) return;
        dotsContainer.innerHTML = '';
        slides.forEach(function (_, i) {
            var dot = document.createElement('button');
            dot.className = 'slider-dot' + (i === 0 ? ' active' : '');
            dot.setAttribute('aria-label', '切换到第 ' + (i + 1) + ' 项');
            dot.addEventListener('click', function () {
                goTo(i);
            });
            dotsContainer.appendChild(dot);
        });
    }

    // ---- 更新圆点 ----
    function updateDots() {
        if (!dotsContainer) return;
        var dots = dotsContainer.querySelectorAll('.slider-dot');
        dots.forEach(function (d, i) {
            d.classList.toggle('active', i === currentIndex);
        });
    }

    // ---- 切换逻辑 ----
    function goTo(index) {
        if (isTransitioning) return;
        if (index === currentIndex) return;
        if (index < 0 || index >= slides.length) return;

        isTransitioning = true;
        resetTimer();

        var currentSlide = slides[currentIndex];
        var nextSlide = slides[index];

        // 重置下一项到初始状态
        nextSlide.style.transition = 'none';
        nextSlide.classList.remove('active');
        void nextSlide.offsetHeight;
        nextSlide.style.transition = '';

        // 当前项淡出
        currentSlide.classList.remove('active');

        // 下一项淡入（双 rAF 确保无闪屏）
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                nextSlide.classList.add('active');
                currentIndex = index;
                updateDots();
                preloadNext();

                setTimeout(function () {
                    isTransitioning = false;
                    handleMedia(nextSlide);
                }, TRANSITION_DURATION);
            });
        });
    }

    // ---- 下一项 ----
    function next() {
        if (slides.length === 0) return;
        goTo((currentIndex + 1) % slides.length);
    }

    // ---- 自动播放 ----
    function startAutoPlay() {
        resetTimer();
    }

    function resetTimer() {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
        if (slides.length < 2) return;

        var currentSlide = slides[currentIndex];
        var video = currentSlide ? currentSlide.querySelector('video') : null;

        if (video) {
            video.removeEventListener('ended', onVideoEnded);
            video.addEventListener('ended', onVideoEnded);
            if (video.ended) {
                onVideoEnded();
            }
        } else {
            timer = setTimeout(next, IMAGE_DURATION);
        }
    }

    function onVideoEnded() {
        if (isTransitioning) return;
        next();
    }

    // ---- 处理媒体 ----
    function handleMedia(slide) {
        var video = slide.querySelector('video');
        if (video) {
            video.removeEventListener('ended', onVideoEnded);
            video.addEventListener('ended', onVideoEnded);
            if (video.paused) {
                video.play().catch(function () {});
            }
        } else {
            resetTimer();
        }
    }

    // ---- 暂停/恢复 ----
    function pauseAutoPlay() {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
    }

    function resumeAutoPlay() {
        if (slides.length < 2) return;
        var currentSlide = slides[currentIndex];
        var video = currentSlide ? currentSlide.querySelector('video') : null;
        if (!video) {
            resetTimer();
        }
    }

    // ---- 绑定事件 ----
    function bindEvents() {
        // 键盘 ← →
        document.addEventListener('keydown', function (e) {
            var rect = slider.getBoundingClientRect();
            var isVisible = rect.bottom > 0 && rect.top < window.innerHeight;
            if (!isVisible) return;
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                if (e.key === 'ArrowLeft') {
                    goTo((currentIndex - 1 + slides.length) % slides.length);
                } else {
                    next();
                }
            }
        });

        // 悬停暂停
        slider.addEventListener('mouseenter', pauseAutoPlay);
        slider.addEventListener('mouseleave', resumeAutoPlay);

        // 触摸滑动
        var touchStartX = 0;
        slider.addEventListener('touchstart', function (e) {
            touchStartX = e.changedTouches[0].screenX;
        }, { passive: true });

        slider.addEventListener('touchend', function (e) {
            var diff = touchStartX - e.changedTouches[0].screenX;
            if (Math.abs(diff) > 50) {
                if (diff > 0) {
                    next();
                } else {
                    goTo((currentIndex - 1 + slides.length) % slides.length);
                }
            }
        }, { passive: true });
    }

    // ---- 启动 ----
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();