/** Tailwind production build for the self-hosted stylesheet (web/vendor/tailwind.css).
 *  Rebuild after changing classes in the pages or scripts:
 *    npx -y tailwindcss@3.4.17 -c tailwind.config.js -i web/tailwind.src.css -o web/vendor/tailwind.css --minify
 *  tests/test_vendor.py checks that every class used in the pages exists in the built file. */
module.exports = {
  content: ['./web/index.html', './web/admin.html', './web/dev.html', './web/verify.html', './web/showcase.html', './web/app.js', './web/admin.js', './p3/sitepages.py'],
  theme: { extend: { screens: { '3xl': '1920px', '4xl': '2560px' } } },
  corePlugins: { preflight: true },
};
