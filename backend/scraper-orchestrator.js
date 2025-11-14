//
// --- ORCHESTRATOR SCRIPT ---
// This script runs both scraping workflows in sequence:
// 1. scraper-prod.js - Scrapes all available timeslots
// 2. scraper-bookings.js - Scrapes user's existing bookings
//

const { spawn } = require('child_process');
const fs = require('fs');

//
// --- HELPER FUNCTIONS ---
//

function runScript(scriptPath, scriptName) {
  return new Promise((resolve, reject) => {
    console.log(`\n${'='.repeat(60)}`);
    console.log(`ORCHESTRATOR: Starting ${scriptName}...`);
    console.log(`${'='.repeat(60)}\n`);

    const startTime = Date.now();
    const child = spawn('node', [scriptPath], {
      stdio: 'inherit',
      cwd: __dirname
    });

    child.on('close', (code) => {
      const endTime = Date.now();
      const duration = ((endTime - startTime) / 1000).toFixed(2);

      if (code === 0) {
        console.log(`\n${'='.repeat(60)}`);
        console.log(`ORCHESTRATOR: ${scriptName} completed successfully in ${duration}s`);
        console.log(`${'='.repeat(60)}\n`);
        resolve({ success: true, scriptName, duration });
      } else {
        console.error(`\n${'='.repeat(60)}`);
        console.error(`ORCHESTRATOR: ${scriptName} failed with exit code ${code}`);
        console.error(`${'='.repeat(60)}\n`);
        reject({ success: false, scriptName, exitCode: code, duration });
      }
    });

    child.on('error', (error) => {
      console.error(`\nORCHESTRATOR: Failed to start ${scriptName}:`, error.message);
      reject({ success: false, scriptName, error: error.message });
    });
  });
}

//
// --- MAIN ORCHESTRATION ---
//

(async () => {
  const orchestrationStartTime = Date.now();
  const results = [];

  console.log('\n' + '='.repeat(60));
  console.log('ORCHESTRATOR: Starting scraping workflows');
  console.log('='.repeat(60));

  try {
    // 1. Run the main scraper (available timeslots)
    const scraperProdResult = await runScript(
      './scraper-prod.js',
      'Room Availability Scraper (scraper-prod.js)'
    );
    results.push(scraperProdResult);

    // 2. Run the bookings scraper (user's existing bookings)
    const scraperBookingsResult = await runScript(
      './scraper-bookings.js',
      'User Bookings Scraper (scraper-bookings.js)'
    );
    results.push(scraperBookingsResult);

    // 3. Run the tasks scraper (user's task list)
    const scraperTasksResult = await runScript(
      './scraper-tasks.js',
      'User Tasks Scraper (scraper-tasks.js)'
    );
    results.push(scraperTasksResult);

    // 3. Create summary report
    const orchestrationEndTime = Date.now();
    const totalDuration = ((orchestrationEndTime - orchestrationStartTime) / 1000).toFixed(2);

    console.log('\n' + '='.repeat(60));
    console.log('ORCHESTRATOR: All workflows completed successfully!');
    console.log('='.repeat(60));
    console.log(`Total duration: ${totalDuration}s`);
    console.log('\nResults summary:');
    results.forEach((result, index) => {
      console.log(`  ${index + 1}. ${result.scriptName}: ${result.duration}s`);
    });
    console.log('='.repeat(60) + '\n');

    // 4. Load and display summary of scraped data
    try {
      const roomsLog = JSON.parse(fs.readFileSync('./log/scraped_log.json', 'utf8'));
      const bookingsLog = JSON.parse(fs.readFileSync('./log/scraped_bookings.json', 'utf8'));
      const tasksLog = JSON.parse(fs.readFileSync('./log/scraped_tasks.json', 'utf8'));

      console.log('\n' + '='.repeat(60));
      console.log('DATA SUMMARY');
      console.log('='.repeat(60));

      if (roomsLog.metadata.success) {
        console.log(`\nRoom Availability (${roomsLog.config.date}):`);
        console.log(`  - Total rooms: ${roomsLog.statistics.total_rooms}`);
        console.log(`  - Available now: ${roomsLog.statistics.available_rooms}`);
        console.log(`  - Partially available: ${roomsLog.statistics.partially_available_rooms}`);
        console.log(`  - Fully booked: ${roomsLog.statistics.booked_rooms}`);
      } else {
        console.log(`\nRoom Availability: FAILED - ${roomsLog.metadata.error}`);
      }

      if (bookingsLog.metadata.success) {
        console.log(`\nUser Bookings:`);
        console.log(`  - Total bookings: ${bookingsLog.statistics.total_bookings}`);
        console.log(`  - Confirmed: ${bookingsLog.statistics.confirmed_bookings}`);
        console.log(`  - Pending: ${bookingsLog.statistics.pending_bookings}`);
        console.log(`  - Total price: $${bookingsLog.statistics.total_price.toFixed(2)}`);
      } else {
        console.log(`\nUser Bookings: FAILED - ${bookingsLog.metadata.error}`);
      }

      if (tasksLog.metadata.success) {
        console.log(`\nUser Tasks:`);
        console.log(`  - Total tasks: ${tasksLog.statistics.total_tasks}`);
        console.log(`  - Pending: ${tasksLog.statistics.pending_tasks}`);
        console.log(`  - Approved: ${tasksLog.statistics.approved_tasks}`);
        console.log(`  - Rejected: ${tasksLog.statistics.rejected_tasks}`);
      } else {
        console.log(`\nUser Tasks: FAILED - ${tasksLog.metadata.error}`);
      }

      console.log('='.repeat(60) + '\n');
    } catch (error) {
      console.log('\nNote: Could not load scraped data for summary');
    }

    process.exit(0);

  } catch (error) {
    const orchestrationEndTime = Date.now();
    const totalDuration = ((orchestrationEndTime - orchestrationStartTime) / 1000).toFixed(2);

    console.error('\n' + '='.repeat(60));
    console.error('ORCHESTRATOR: Workflow failed!');
    console.error('='.repeat(60));
    console.error(`Failed workflow: ${error.scriptName || 'Unknown'}`);
    console.error(`Total duration: ${totalDuration}s`);
    console.error('='.repeat(60) + '\n');

    process.exit(1);
  }
})();
