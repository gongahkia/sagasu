// Simple test script to debug Playwright and network connectivity

const { chromium } = require('playwright');

(async () => {
  console.log('🚀 Starting Playwright test...');

  try {
    // Launch browser with visible window
    console.log('1. Launching browser...');
    const browser = await chromium.launch({
      headless: false,
      timeout: 60000  // 60 second timeout
    });
    console.log('✓ Browser launched successfully');

    // Create context and page
    console.log('2. Creating new page...');
    const context = await browser.newContext();
    const page = await context.newPage();
    console.log('✓ Page created');

    // Test 1: Try Google (simple test)
    console.log('3. Testing with Google...');
    await page.goto('https://www.google.com', {
      waitUntil: 'networkidle',
      timeout: 30000
    });
    console.log('✓ Google loaded successfully');
    await page.waitForTimeout(2000);

    // Test 2: Try SMU website
    console.log('4. Testing SMU FBS website...');
    const smuUrl = 'https://www.smubondue.com/facility-booking-system-fbs';
    console.log(`   Navigating to: ${smuUrl}`);

    await page.goto(smuUrl, {
      waitUntil: 'domcontentloaded',  // Less strict than networkidle
      timeout: 60000  // 60 seconds
    });
    console.log('✓ SMU FBS page loaded');

    // Wait a bit to see what's on the page
    console.log('5. Waiting 5 seconds for page to fully load...');
    await page.waitForTimeout(5000);

    // Take screenshot for debugging
    await page.screenshot({ path: './log/screenshot/test-screenshot.png' });
    console.log('✓ Screenshot saved to ./log/screenshot/test-screenshot.png');

    // Get page title
    const title = await page.title();
    console.log(`   Page title: "${title}"`);

    console.log('\n✅ All tests passed!');
    console.log('The browser will stay open for 10 seconds so you can inspect...');
    await page.waitForTimeout(10000);

    await browser.close();
    console.log('Browser closed.');

  } catch (error) {
    console.error('\n❌ Error occurred:');
    console.error('Error name:', error.name);
    console.error('Error message:', error.message);

    if (error.name === 'TimeoutError') {
      console.error('\n💡 Troubleshooting tips:');
      console.error('1. Check your internet connection');
      console.error('2. Try accessing the URL in a regular browser:');
      console.error('   https://www.smubondue.com/facility-booking-system-fbs');
      console.error('3. The website might be down or slow');
      console.error('4. You might need to be on SMU network/VPN');
    }

    process.exit(1);
  }
})();
