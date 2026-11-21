const firstError = document.querySelector('main form [aria-invalid="true"]');
if (firstError) firstError.focus();

for (const region of document.querySelectorAll('[data-scroll-label]')) {
    const updateScrollRegion = () => {
        const scrollable = region.scrollWidth > region.clientWidth || region.scrollHeight > region.clientHeight;
        region.tabIndex = scrollable ? 0 : -1;
        region.setAttribute('role', 'region');
        region.setAttribute('aria-label', region.dataset.scrollLabel);
    };
    const observer = new ResizeObserver(updateScrollRegion);
    observer.observe(region);
    if (region.firstElementChild) observer.observe(region.firstElementChild);
    updateScrollRegion();
}

document.addEventListener('shown.bs.modal', (event) => {
    event.target.querySelector('[data-modal-focus]')?.focus();
});
