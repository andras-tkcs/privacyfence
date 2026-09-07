(() => {
  const format = new Intl.NumberFormat('en');

  fetch('./release-stats.json', { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error('stats unavailable');
      return response.json();
    })
    .then((data) => {
      const releaseMeta = document.getElementById('release-meta');
      if (releaseMeta && data.latest_release) {
        const version = document.createElement('span');
        version.textContent = `Latest ${data.latest_release}`;
        releaseMeta.prepend(version);
      }

      // Social proof should help, not advertise an empty launch.
      // Show the counters only once they are meaningful.
      if ((data.downloads || 0) >= 50 || (data.stars || 0) >= 10) {
        document.getElementById('download-count').textContent = format.format(data.downloads || 0);
        document.getElementById('star-count').textContent = format.format(data.stars || 0);
        document.getElementById('stats').hidden = false;
      }
    })
    .catch(() => {
      // Statistics are enhancement-only. The site remains fully functional
      // if metadata generation or loading fails.
    });
})();
