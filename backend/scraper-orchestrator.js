//
// --- ORCHESTRATOR SCRIPT ---
// This script runs both scraping workflows in sequence:
// 1. scraper-prod.js - Scrapes all available timeslots
// 2. scraper-bookings.js - Scrapes user's existing bookings
//

const { spawn } = require('child_process');
const fs = require('fs');
const { logger } = require('./logger');

const LOG_FILE_PATH = './log/scraper_console.txt';

function ensureLogDirExists() {
  try {
    fs.mkdirSync('./log', { recursive: true });
  } catch {
    // ignore
  }
}

function createLogStream() {
  ensureLogDirExists();
  const stream = fs.createWriteStream(LOG_FILE_PATH, { flags: 'w' });
  stream.write(`Sagasu scraper console log\n`);
  stream.write(`Started: ${new Date().toISOString()}\n`);
  stream.write(`${'='.repeat(60)}\n\n`);
  return stream;
}

function teeWrite(stream, chunk) {
  try {
    stream.write(chunk);
  } catch {
    // ignore
  }
}

//
// --- HELPER FUNCTIONS ---
//

function runScript(scriptPath, scriptName) {
  return new Promise((resolve, reject) => {
    logger.info(`\n${'='.repeat(60)}`);
    logger.info(`ORCHESTRATOR: Starting ${scriptName}...`);
    logger.info(`${'='.repeat(60)}\n`);

    const startTime = Date.now();
    const child = spawn('node', [scriptPath], {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: __dirname
    });

    child.stdout.on('data', (chunk) => {
      process.stdout.write(chunk);
    });

    child.stderr.on('data', (chunk) => {
      process.stderr.write(chunk);
    });

    child.on('close', (code) => {
      const endTime = Date.now();
      const duration = ((endTime - startTime) / 1000).toFixed(2);

      if (code === 0) {
        logger.info(`\n${'='.repeat(60)}`);
        logger.info(`ORCHESTRATOR: ${scriptName} completed successfully in ${duration}s`);
        logger.info(`${'='.repeat(60)}\n`);
        resolve({ success: true, scriptName, duration });
      } else {
        logger.error(`\n${'='.repeat(60)}`);
        logger.error(`ORCHESTRATOR: ${scriptName} failed with exit code ${code}`);
        logger.error(`${'='.repeat(60)}\n`);
        reject({ success: false, scriptName, exitCode: code, duration });
      }
    });

    child.on('error', (error) => {
      logger.error(`\nORCHESTRATOR: Failed to start ${scriptName}:`, error.message);
      reject({ success: false, scriptName, error: error.message });
    });
  });
}

//
// --- MAIN ORCHESTRATION ---
//

(async () => {
  const logStream = createLogStream();

  const originalStdoutWrite = process.stdout.write.bind(process.stdout);
  const originalStderrWrite = process.stderr.write.bind(process.stderr);

  process.stdout.write = (chunk, encoding, cb) => {
    teeWrite(logStream, chunk);
    return originalStdoutWrite(chunk, encoding, cb);
  };

  process.stderr.write = (chunk, encoding, cb) => {
    teeWrite(logStream, chunk);
    return originalStderrWrite(chunk, encoding, cb);
  };

  const closeLog = () => {
    try {
      logStream.end(`\nFinished: ${new Date().toISOString()}\n`);
    } catch {
      // ignore
    }
  };

  const orchestrationStartTime = Date.now();
  const results = [];

  logger.info('\n' + '='.repeat(60));
  logger.info('ORCHESTRATOR: Starting scraping workflows');
  logger.info('='.repeat(60));

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

    logger.info('\n' + '='.repeat(60));
    logger.info('ORCHESTRATOR: All workflows completed successfully!');
    logger.info('='.repeat(60));
    logger.info(`Total duration: ${totalDuration}s`);
    logger.info('\nResults summary:');
    results.forEach((result, index) => {
      logger.info(`  ${index + 1}. ${result.scriptName}: ${result.duration}s`);
    });
    logger.info('='.repeat(60) + '\n');

    // 4. Load and display summary of scraped data
    try {
      const roomsLog = JSON.parse(fs.readFileSync('./log/scraped_log.json', 'utf8'));
      const bookingsLog = JSON.parse(fs.readFileSync('./log/scraped_bookings.json', 'utf8'));
      const tasksLog = JSON.parse(fs.readFileSync('./log/scraped_tasks.json', 'utf8'));

      logger.info('\n' + '='.repeat(60));
      logger.info('DATA SUMMARY');
      logger.info('='.repeat(60));

      if (roomsLog.metadata.success) {
        logger.info(`\nRoom Availability (${roomsLog.config.date}):`);
        logger.info(`  - Total rooms: ${roomsLog.statistics.total_rooms}`);
        logger.info(`  - Available now: ${roomsLog.statistics.available_rooms}`);
        logger.info(`  - Partially available: ${roomsLog.statistics.partially_available_rooms}`);
        logger.info(`  - Fully booked: ${roomsLog.statistics.booked_rooms}`);
      } else {
        logger.info(`\nRoom Availability: FAILED - ${roomsLog.metadata.error}`);
      }

      if (bookingsLog.metadata.success) {
        logger.info(`\nUser Bookings:`);
        logger.info(`  - Total bookings: ${bookingsLog.statistics.total_bookings}`);
        logger.info(`  - Confirmed: ${bookingsLog.statistics.confirmed_bookings}`);
        logger.info(`  - Pending: ${bookingsLog.statistics.pending_bookings}`);
        logger.info(`  - Total price: $${bookingsLog.statistics.total_price.toFixed(2)}`);
      } else {
        logger.info(`\nUser Bookings: FAILED - ${bookingsLog.metadata.error}`);
      }

      if (tasksLog.metadata.success) {
        logger.info(`\nUser Tasks:`);
        logger.info(`  - Total tasks: ${tasksLog.statistics.total_tasks}`);
        logger.info(`  - Pending: ${tasksLog.statistics.pending_tasks}`);
        logger.info(`  - Approved: ${tasksLog.statistics.approved_tasks}`);
        logger.info(`  - Rejected: ${tasksLog.statistics.rejected_tasks}`);
      } else {
        logger.info(`\nUser Tasks: FAILED - ${tasksLog.metadata.error}`);
      }

      logger.info('='.repeat(60) + '\n');
    } catch (error) {
      logger.info('\nNote: Could not load scraped data for summary');
    }

    closeLog();
    process.exit(0);

  } catch (error) {
    const orchestrationEndTime = Date.now();
    const totalDuration = ((orchestrationEndTime - orchestrationStartTime) / 1000).toFixed(2);

    logger.error('\n' + '='.repeat(60));
    logger.error('ORCHESTRATOR: Workflow failed!');
    logger.error('='.repeat(60));
    logger.error(`Failed workflow: ${error.scriptName || 'Unknown'}`);
    logger.error(`Total duration: ${totalDuration}s`);
    logger.error('='.repeat(60) + '\n');

    closeLog();
    process.exit(1);
  }
})();
