(function () {
  "use strict";
  function afetch(url, opts) {
    const fn = window.__ublAuthFetch;
    if (!fn) throw new Error("auth not ready");
    return fn(url, opts);
  }
  function lg(msg) {
    const fn = window.__ublLog;
    if (fn) fn(msg);
  }
  let analysisPollingInterval = null;
  let analysisCountdownInterval = null;
  let currentAnalysisCallId = null;

  function setDownloadButtonsVisible(show) {
    ["downloadAnalysisBtn", "downloadTranscriptBtn", "modalDownloadAnalysisBtn", "modalDownloadTranscriptBtn"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (show) el.classList.remove("hidden");
      else el.classList.add("hidden");
    });
  }

  async function downloadAnalysis(callId) {
    if (!callId) return;
    try {
      const r = await afetch("/call-analysis/" + encodeURIComponent(callId) + "/download");
      if (!r.ok) { lg("Download analysis failed"); return; }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = callId + "_analysis.json";
      a.click();
      URL.revokeObjectURL(url);
      lg("Downloaded analysis JSON");
    } catch (e) {
      lg("Download analysis failed");
    }
  }

  async function downloadTranscript(callId) {
    if (!callId) return;
    try {
      const r = await afetch("/call-transcript/" + encodeURIComponent(callId) + "/download");
      if (!r.ok) { lg("Transcript not available yet"); return; }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = callId + "_transcript.json";
      a.click();
      URL.revokeObjectURL(url);
      lg("Downloaded transcript JSON");
    } catch (e) {
      lg("Download transcript failed");
    }
  }

  function openAnalysisModal() {
    const m = document.getElementById("analysisModal");
    if (m) { m.style.display = "flex"; document.body.style.overflow = "hidden"; }
  }
  function closeAnalysisModal() {
    const m = document.getElementById("analysisModal");
    if (m) { m.style.display = "none"; document.body.style.overflow = "auto"; }
  }
  window.closeUblAnalysisModal = closeAnalysisModal;

// Function to fetch and display call analysis
async function fetchCallAnalysis(callId, showInModal = false, isAutoLoad = false) {

  if (!callId) {
    return false;
  }

  const targetContainer = showInModal
    ? document.getElementById("modalAnalysisContent")
    : document.getElementById("analysisContent");
  const analysisContainer = document.getElementById("analysisContainer");


  // Show loading state only if not auto-loading
  if (!isAutoLoad) {
    if (showInModal) {
      targetContainer.innerHTML = `
        <div class="text-center py-8 text-gray-500">
          <svg class="w-12 h-12 mx-auto mb-3 animate-spin text-ubl-blue" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <div class="text-sm font-semibold">Loading analysis for call ${callId}...</div>
        </div>
      `;
      openAnalysisModal();
    } else {
      analysisContainer.classList.remove("hidden");
      targetContainer.innerHTML = `
        <div style="text-align: center; padding: 30px; color: #718096">
          <svg class="w-12 h-12 mx-auto mb-3 animate-spin text-ubl-blue" style="margin: 0 auto 10px auto; display: inline-block;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <div style="font-size: 13px; font-weight: 600">Loading analysis for call ${callId}...</div>
        </div>
      `;
    }
  }

  if (!isAutoLoad) {
    lg(`📊 Fetching call analysis for: ${callId}`);
  }

  try {
    const response = await afetch(`/call-analysis/${callId}`);

    if (response.ok) {
      const analysisData = await response.json();
      lg("✅ Call analysis retrieved successfully");
      currentAnalysisCallId = callId;
      setDownloadButtonsVisible(true);
      displayCallAnalysisInContainer(analysisData, targetContainer);

      // Stop polling if active
      if (analysisPollingInterval) {
        clearInterval(analysisPollingInterval);
        analysisPollingInterval = null;
      }
      if (analysisCountdownInterval) {
        clearInterval(analysisCountdownInterval);
        analysisCountdownInterval = null;
      }

      return true; // Success
    } else if (response.status === 404) {
      if (!isAutoLoad) {
        lg("⚠️ Analysis not yet available");
      }
      return false; // Not ready yet
    } else {
      lg("❌ Error fetching analysis");
      targetContainer.innerHTML = `
        <div class="text-center p-8 rounded-xl border-2 border-red-500 bg-gradient-to-br from-red-50 to-red-100">
          <svg class="w-16 h-16 mx-auto mb-4 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div class="text-lg font-bold mb-2 text-red-900">Error Loading Analysis</div>
          <div class="text-sm text-red-800">
            Failed to fetch call analysis. Please try again.
          </div>
        </div>
      `;
      return false;
    }
  } catch (error) {
    console.error("Error fetching call analysis:", error);
    lg("❌ Failed to fetch call analysis");
    if (!isAutoLoad) {
      targetContainer.innerHTML = `
        <div class="text-center p-8 rounded-xl border-2 border-red-500 bg-gradient-to-br from-red-50 to-red-100">
          <svg class="w-16 h-16 mx-auto mb-4 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <div class="text-lg font-bold mb-2 text-red-900">Network Error</div>
          <div class="text-sm text-red-800">${error.message}</div>
        </div>
      `;
    }
    return false;
  }
}

// Function to start analysis countdown and polling
function startAnalysisCountdown(callId) {
  const analysisContainer = document.getElementById("analysisContainer");
  const analysisContent = document.getElementById("analysisContent");

  // Show analysis container
  analysisContainer.classList.remove("hidden");
  setDownloadButtonsVisible(false);
  currentAnalysisCallId = null;

  // Hide "Load Last Call" button during analysis
  const _llb = document.getElementById("loadLastCallBtn");
  if (_llb) { _llb.classList.add("hidden"); _llb.classList.remove("flex"); }

  // Clear any existing intervals
  if (analysisCountdownInterval) {
    clearInterval(analysisCountdownInterval);
  }
  if (analysisPollingInterval) {
    clearInterval(analysisPollingInterval);
  }

  let secondsRemaining = 60;

  // Update countdown display
  function updateCountdown() {
    const minutes = Math.floor(secondsRemaining / 60);
    const seconds = secondsRemaining % 60;
    const timeString = `${minutes}:${String(seconds).padStart(2, "0")}`;

    analysisContent.innerHTML = `
      <div class="text-center p-8 rounded-xl border-2 border-ubl-blue bg-gradient-to-br from-green-50 to-green-100">
        <svg class="w-16 h-16 mx-auto mb-4 text-ubl-blue" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <div class="text-lg font-bold mb-2 text-blue-900">Analysis in Progress</div>
        <div class="text-sm text-blue-800 mb-4">
          Your call is being analyzed by AI.<br/>
          Analysis will be ready in approximately <span class="font-mono font-bold text-lg">${timeString}</span>
        </div>
        <div class="w-full bg-blue-200 rounded-full h-2 overflow-hidden">
          <div class="bg-gradient-to-r from-ubl-blue to-ubl-light-blue h-full rounded-full transition-all duration-1000" 
               style="width: ${((60 - secondsRemaining) / 60) * 100}%"></div>
        </div>
      </div>
    `;
  }

  // Initial display
  updateCountdown();
  lg(`⏳ Analysis will be ready in 60 seconds...`);

  // Start countdown
  analysisCountdownInterval = setInterval(() => {
    secondsRemaining--;

    if (secondsRemaining > 0) {
      updateCountdown();
    } else {
      // Countdown finished, start polling
      clearInterval(analysisCountdownInterval);
      analysisCountdownInterval = null;
      startAnalysisPolling(callId);
    }
  }, 1000);
}

// Function to poll for analysis with retry
async function startAnalysisPolling(callId) {
  const analysisContent = document.getElementById("analysisContent");

  // Show fetching state
  analysisContent.innerHTML = `
    <div class="text-center p-8 rounded-xl border-2 border-green-500 bg-gradient-to-br from-green-50 to-green-100">
      <svg class="w-16 h-16 mx-auto mb-4 animate-spin text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
      </svg>
      <div class="text-lg font-bold mb-2 text-green-900">Fetching Analysis</div>
      <div class="text-sm text-green-800">
        Retrieving your call analysis...<br/>
        <span class="text-xs">Will retry every 10 seconds if not ready</span>
      </div>
    </div>
  `;

  lg(`📡 Fetching call analysis...`);

  // Try to fetch immediately
  const success = await fetchCallAnalysis(callId, false, true);

  if (!success) {
    // If not successful, start polling every 10 seconds
    lg(`⏳ Analysis not ready, will retry every 10 seconds...`);

    let retryCount = 0;
    analysisPollingInterval = setInterval(async () => {
      retryCount++;

      // Update the UI to show retry count
      analysisContent.innerHTML = `
        <div class="text-center p-8 rounded-xl border-2 border-amber-500 bg-gradient-to-br from-amber-50 to-amber-100">
          <svg class="w-16 h-16 mx-auto mb-4 animate-spin text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <div class="text-lg font-bold mb-2 text-amber-900">Still Processing</div>
          <div class="text-sm text-amber-800 mb-2">
            Analysis is taking longer than expected...<br/>
            <span class="text-xs">Retry attempt: ${retryCount}</span>
          </div>
          <div class="flex items-center justify-center gap-1 mt-3">
            <div class="w-2 h-2 bg-amber-600 rounded-full animate-bounce" style="animation-delay: 0s"></div>
            <div class="w-2 h-2 bg-amber-600 rounded-full animate-bounce" style="animation-delay: 0.2s"></div>
            <div class="w-2 h-2 bg-amber-600 rounded-full animate-bounce" style="animation-delay: 0.4s"></div>
          </div>
        </div>
      `;

      const success = await fetchCallAnalysis(callId, false, true);

      if (success) {
        // Analysis retrieved successfully, stop polling
        clearInterval(analysisPollingInterval);
        analysisPollingInterval = null;
      }
    }, 10000); // Retry every 10 seconds
  }
}

// Function to display call analysis in a container (moved from inline code)
function displayCallAnalysisInContainer(analysisData, targetContainer) {

  if (!analysisData) {
    targetContainer.innerHTML =
      '<p class="text-center text-gray-500 py-10">No analysis data available.</p>';
    return;
  }

  let htmlContent = "";
  let cardIndex = 0;

  // Helper function to detect if a value is a percentage or score
  const isPercentage = (key, value) => {
    const keyLower = key.toLowerCase();

    // Explicitly include accuracy-related fields
    const includeFields = ["accuracy", "score", "confidence", "percentage", "rate", "ratio"];
    const isIncluded = includeFields.some((field) => keyLower.includes(field));

    // Exclude specific fields that shouldn't be progress bars
    const excludeFields = [
      "duration",
      "time",
      "timestamp",
      "id",
      "count",
      "total",
      "year",
      "month",
      "day",
    ];
    const isExcluded = excludeFields.some((field) => keyLower.includes(field));

    const result = typeof value === "number" && !isExcluded && value >= 0 && value <= 100;
    return result;
  };

  // Helper function to create animated progress bar
  const createProgressBar = (value, label, gradient) => {
    const percentage = value > 1 ? value : value * 100;
    const displayValue = percentage.toFixed(1);
    const animationDelay = cardIndex * 0.08;
    cardIndex++;

    // Check if lower values are better for this metric
    const labelLower = label.toLowerCase();
    const lowerIsBetter = [
      "error",
      "complaint",
      "issue",
      "problem",
      "failure",
      "reject",
      "negative",
      "abandoned",
      "dropped",
      "missed",
      "wait",
      "delay",
      "escalation",
      "transfer",
      "repeat",
      "churn",
      "dissatisfaction",
      "dispute",
      "fraud",
      "violation",
      "breach",
      "risk",
      "loss",
      "cost",
      "expense",
      "duration",
      "time",
      "latency",
      "bounce",
    ].some((keyword) => labelLower.includes(keyword));

    // Dynamic color based on percentage and whether lower is better
    let dynamicGradient;
    let textColor;

    if (lowerIsBetter) {
      // Reverse color scheme: lower is better
      if (percentage < 30) {
        // Green for low values (good)
        dynamicGradient = "linear-gradient(135deg, #48bb78 0%, #38a169 100%)";
        textColor = "#2f855a";
      } else if (percentage >= 30 && percentage < 70) {
        // Orange/Yellow for medium values
        dynamicGradient = "linear-gradient(135deg, #f6ad55 0%, #ed8936 100%)";
        textColor = "#c05621";
      } else {
        // Red for high values >= 70 (bad)
        dynamicGradient = "linear-gradient(135deg, #f56565 0%, #e53e3e 100%)";
        textColor = "#c53030";
      }
    } else {
      // Normal color scheme: higher is better
      if (percentage < 30) {
        // Red for low values (bad)
        dynamicGradient = "linear-gradient(135deg, #f56565 0%, #e53e3e 100%)";
        textColor = "#c53030";
      } else if (percentage >= 30 && percentage < 70) {
        // Orange/Yellow for medium values
        dynamicGradient = "linear-gradient(135deg, #f6ad55 0%, #ed8936 100%)";
        textColor = "#c05621";
      } else {
        // Green for high values >= 70 (good)
        dynamicGradient = "linear-gradient(135deg, #48bb78 0%, #38a169 100%)";
        textColor = "#2f855a";
      }
    }

    return `
      <div style="padding: 14px; background: linear-gradient(135deg, #f8fafc 0%, #ffffff 100%); border-radius: 12px; border: 1px solid #e2e8f0; position: relative; transition: all 0.3s ease;" 
        onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 12px rgba(0,0,0,0.08)'" 
        onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='none'" 
        title="${label}: ${displayValue}%">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <span style="font-weight: 600; color: #2d3748; font-size: 13px;">${label}</span>
          <span style="font-size: 16px; color: ${textColor}; font-weight: 700; font-family: 'Courier New', monospace;">${displayValue}%</span>
        </div>
        <div style="background: linear-gradient(135deg, #e2e8f0 0%, #cbd5e0 100%); border-radius: 10px; height: 10px; overflow: hidden; position: relative; box-shadow: inset 0 1px 3px rgba(0,0,0,0.1);">
          <div class="progress-fill" style="width: 0%; height: 100%; background: ${dynamicGradient}; border-radius: 10px; transition: width 1.8s cubic-bezier(0.34, 1.56, 0.64, 1) ${animationDelay}s; position: relative; overflow: hidden; box-shadow: 0 0 8px rgba(0,0,0,0.15);" data-target="${percentage}">
            <div style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent); animation: shimmer 2s infinite;"></div>
            <div style="position: absolute; top: 0; right: 0; bottom: 0; width: 30%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2));"></div>
          </div>
        </div>
      </div>
    `;
  };

  // Helper function to render nested objects/arrays beautifully
  const renderValue = (value, depth = 0) => {
    if (value === null || value === undefined) {
      return '<span style="color: #a0aec0; font-style: italic;">N/A</span>';
    }

    if (Array.isArray(value)) {
      if (value.length === 0)
        return '<span style="color: #a0aec0; font-style: italic;">Empty list</span>';
      return `
        <ul style="margin: 4px 0; padding-left: 12px; list-style: none;">
          ${value
            .map(
              (item) => `
            <li style="margin-bottom: 4px; padding: 6px 8px; background: ${
              depth === 0 ? "#f7fafc" : "#edf2f7"
            }; border-radius: 6px; border-left: 2px solid #667eea; position: relative; padding-left: 20px;">
              <span style="position: absolute; left: 8px; top: 6px; color: #667eea; font-weight: bold; font-size: 12px;">•</span>
              ${typeof item === "object" ? renderValue(item, depth + 1) : item}
            </li>
          `
            )
            .join("")}
        </ul>
      `;
    }

    if (typeof value === "object") {
      // Check if this object contains mostly percentages (quality metrics)
      const entries = Object.entries(value);
      const percentageCount = entries.filter(([k, v]) => isPercentage(k, v)).length;
      const isMetricsObject = percentageCount > 0 && percentageCount / entries.length > 0.5;

      if (isMetricsObject) {
        // Render as grid of progress bars

        return `
          <div class="metrics-grid" style="display: grid; grid-template-columns: repeat(1, 1fr); gap: 12px; margin: 4px 0;">
            ${entries
              .map(([k, v], idx) => {
                if (isPercentage(k, v)) {
                  return createProgressBar(v, k.replace(/_/g, " "), ""); // Empty gradient, will use dynamic colors
                } else {
                  return `
                    <div style="padding: 10px; background: linear-gradient(135deg, #f8fafc 0%, #ffffff 100%); border-radius: 8px; border: 1px solid #e2e8f0;">
                      <div style="font-weight: 600; color: #4a5568; margin-bottom: 3px; font-size: 11px; text-transform: capitalize;">
                        ${k.replace(/_/g, " ")}
                      </div>
                      <div style="color: #2d3748; font-size: 11px;">
                        ${renderValue(v, depth + 1)}
                      </div>
                    </div>
                  `;
                }
              })
              .join("")}
          </div>
        `;
      } else {
        // Regular nested object rendering
        return `
          <div style="background: ${
            depth === 0 ? "#f8fafc" : "#edf2f7"
          }; border-radius: 8px; padding: 10px; margin: 4px 0; border: 1px solid #e2e8f0;">
            ${entries
              .map(([k, v]) => {
                return `
                    <div style="margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid #e2e8f0;">
                      <div style="font-weight: 600; color: #4a5568; margin-bottom: 3px; font-size: 11px; text-transform: capitalize;">
                        ${k.replace(/_/g, " ")}
                      </div>
                      <div style="color: #2d3748; font-size: 11px; margin-left: 8px;">
                        ${renderValue(v, depth + 1)}
                      </div>
                    </div>
                  `;
              })
              .join("")}
          </div>
        `;
      }
    }

    if (typeof value === "boolean") {
      return `<span style="display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; background: ${
        value ? "#c6f6d5" : "#fed7d7"
      }; color: ${
        value ? "#22543d" : "#742a2a"
      }; border-radius: 12px; font-weight: 600; font-size: 10px;">
        <span style="font-size: 12px;">${value ? "✓" : "✗"}</span> ${value ? "Yes" : "No"}
      </span>`;
    }

    return `<span style="color: #2d3748; font-weight: 500; font-size: 11px;">${value}</span>`;
  };

  // Helper function to create a card with animation
  const createCard = (
    title,
    content,
    icon = "📄",
    gradient = "linear-gradient(135deg, #667eea 0%, #764ba2 100%)"
  ) => {
    const delay = cardIndex * 0.06;
    cardIndex++;

    return `
      <div class="analysis-card" style="
        background: white;
        border-radius: 10px;
        padding: 12px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        border: 1px solid #e2e8f0;
        position: relative;
        overflow: hidden;
        opacity: 0;
        transform: translateY(15px);
        animation: slideInUp 0.4s cubic-bezier(0.4, 0, 0.2, 1) ${delay}s forwards;
      ">
        <div style="position: absolute; top: 0; left: 0; right: 0; height: 3px; background: ${gradient};"></div>
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
          <div style="
            width: 28px;
            height: 28px;
            background: ${gradient};
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            box-shadow: 0 2px 6px rgba(102, 126, 234, 0.25);
          ">${icon}</div>
          <h4 style="margin: 0; color: #1a202c; font-size: 13px; font-weight: 700;">${title}</h4>
        </div>
        <div style="color: #4a5568; font-size: 12px; line-height: 1.5;">
          ${content}
        </div>
      </div>
    `;
  };

  // Summary card
  if (analysisData.summary) {
    htmlContent += createCard(
      "Call Summary",
      `<p style="margin: 0; font-size: 12px; line-height: 1.5; color: #2d3748;">${analysisData.summary}</p>`,
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>',
      "linear-gradient(135deg, #667eea 0%, #764ba2 100%)"
    );
  }

  // Sentiment card with visual indicator
  if (analysisData.sentiment) {
    const sentiment = analysisData.sentiment.toLowerCase();
    let sentimentIcon =
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>';
    let sentimentDisplay = "😐";
    let sentimentGradient = "linear-gradient(135deg, #a8a8a8 0%, #7a7a7a 100%)";
    let sentimentBg = "#e2e8f0";
    let sentimentColor = "#4a5568";

    if (
      sentiment.includes("positive") ||
      sentiment.includes("satisfied") ||
      sentiment.includes("happy")
    ) {
      sentimentIcon =
        '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>';
      sentimentDisplay = "😊";
      sentimentGradient = "linear-gradient(135deg, #48bb78 0%, #38a169 100%)";
      sentimentBg = "#c6f6d5";
      sentimentColor = "#22543d";
    } else if (
      sentiment.includes("negative") ||
      sentiment.includes("upset") ||
      sentiment.includes("angry")
    ) {
      sentimentIcon =
        '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>';
      sentimentDisplay = "😔";
      sentimentGradient = "linear-gradient(135deg, #f56565 0%, #e53e3e 100%)";
      sentimentBg = "#fed7d7";
      sentimentColor = "#742a2a";
    } else if (sentiment.includes("neutral")) {
      sentimentIcon =
        '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>';
      sentimentDisplay = "😐";
      sentimentGradient = "linear-gradient(135deg, #4299e1 0%, #3182ce 100%)";
      sentimentBg = "#bee3f8";
      sentimentColor = "#2c5282";
    }

    htmlContent += createCard(
      "Customer Sentiment",
      `<div style="display: flex; align-items: center; gap: 10px;">
        <div style="font-size: 28px;">${sentimentDisplay}</div>
        <div style="flex: 1;">
          <div style="font-size: 14px; font-weight: 700; color: ${sentimentColor}; margin-bottom: 4px;">
            ${analysisData.sentiment}
          </div>
          <div style="display: inline-block; padding: 4px 10px; background: ${sentimentBg}; color: ${sentimentColor}; border-radius: 12px; font-weight: 600; font-size: 10px;">
            Call Tone
          </div>
        </div>
      </div>`,
      sentimentIcon,
      sentimentGradient
    );
  }

  // Key Points card
  if (
    analysisData.key_points &&
    Array.isArray(analysisData.key_points) &&
    analysisData.key_points.length > 0
  ) {
    const keyPointsContent = `
      <div style="display: grid; gap: 6px;">
        ${analysisData.key_points
          .map(
            (point, idx) => `
          <div style="
            padding: 8px 10px 8px 32px;
            background: linear-gradient(135deg, #f7fafc 0%, #ffffff 100%);
            border-radius: 8px;
            border-left: 3px solid #4facfe;
            position: relative;
            transition: transform 0.2s;
          " onmouseover="this.style.transform='translateX(2px)'" onmouseout="this.style.transform='translateX(0)'">
            <div style="
              position: absolute;
              left: 8px;
              top: 50%;
              transform: translateY(-50%);
              width: 18px;
              height: 18px;
              background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
              border-radius: 50%;
              display: flex;
              align-items: center;
              justify-content: center;
              color: white;
              font-weight: 700;
              font-size: 9px;
            ">${idx + 1}</div>
            <span style="color: #2d3748; font-size: 11px; line-height: 1.4;">${point}</span>
          </div>
        `
          )
          .join("")}
      </div>
    `;
    htmlContent += createCard(
      "Key Discussion Points",
      keyPointsContent,
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" /></svg>',
      "linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)"
    );
  }

  // Action Items card
  if (
    analysisData.action_items &&
    Array.isArray(analysisData.action_items) &&
    analysisData.action_items.length > 0
  ) {
    const actionItemsContent = `
      <div style="display: grid; gap: 6px;">
        ${analysisData.action_items
          .map(
            (item) => `
          <div style="
            padding: 8px 10px;
            background: linear-gradient(135deg, #f0fff4 0%, #ffffff 100%);
            border-radius: 8px;
            display: flex;
            align-items: start;
            gap: 8px;
            border: 1.5px solid #c6f6d5;
            transition: all 0.2s;
          " onmouseover="this.style.borderColor='#48bb78'; this.style.transform='translateX(2px)'" onmouseout="this.style.borderColor='#c6f6d5'; this.style.transform='translateX(0)'">
            <div style="
              width: 16px;
              height: 16px;
              background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
              border-radius: 50%;
              display: flex;
              align-items: center;
              justify-content: center;
              flex-shrink: 0;
              color: white;
              font-weight: 700;
              font-size: 10px;
            ">✓</div>
            <span style="color: #2d3748; font-size: 11px; line-height: 1.4; font-weight: 500;">${item}</span>
          </div>
        `
          )
          .join("")}
      </div>
    `;
    htmlContent += createCard(
      "Action Items",
      actionItemsContent,
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>',
      "linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)"
    );
  }

  // Process all other fields with intelligent rendering
  const processedKeys = ["summary", "sentiment", "key_points", "action_items", "call_id"];
  const remainingFields = Object.keys(analysisData).filter((key) => !processedKeys.includes(key));

  // Group metrics (numbers) separately for progress bars
  const metrics = {};
  const otherFields = {};

  remainingFields.forEach((key) => {
    const value = analysisData[key];
    if (isPercentage(key, value)) {
      metrics[key] = value;
    } else {
      otherFields[key] = value;
    }
  });


  // Display metrics with progress bars in responsive grid
  if (Object.keys(metrics).length > 0) {
    const metricsContent = Object.entries(metrics)
      .map(([key, value]) => {
        const label = key.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
        return createProgressBar(value, label, ""); // Empty gradient, will use dynamic colors
      })
      .join("");

    htmlContent += createCard(
      "Performance Metrics",
      `<div style="display: grid; grid-template-columns: repeat(1, 1fr); gap: 12px; padding: 6px 0;">
        <style>
          @media (min-width: 640px) {
            .metrics-grid {
              grid-template-columns: repeat(2, 1fr) !important;
            }
          }
        </style>
        <div class="metrics-grid" style="display: grid; grid-template-columns: repeat(1, 1fr); gap: 12px;">
          ${metricsContent}
        </div>
      </div>`,
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>',
      "linear-gradient(135deg, #667eea 0%, #764ba2 100%)"
    );
  } else {
  }

  // Display other fields with intelligent rendering
  Object.entries(otherFields).forEach(([key, value]) => {
    const formattedKey = key.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
    const icons = [
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" /></svg>',
      '<svg style="width:16px;height:16px;color:white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" /></svg>',
    ];
    const gradients = [
      "linear-gradient(135deg, #fa709a 0%, #fee140 100%)",
      "linear-gradient(135deg, #30cfd0 0%, #330867 100%)",
      "linear-gradient(135deg, #a8edea 0%, #fed6e3 100%)",
      "linear-gradient(135deg, #ff9a9e 0%, #fecfef 100%)",
      "linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%)",
    ];
    const icon = icons[Object.keys(otherFields).indexOf(key) % icons.length];
    const gradient = gradients[Object.keys(otherFields).indexOf(key) % gradients.length];

    htmlContent += createCard(formattedKey, renderValue(value), icon, gradient);
  });


  // Wrap content in a grid container
  const wrappedContent = htmlContent
    ? `<div class="grid grid-cols-1 gap-3">${htmlContent}</div>`
    : '<p class="text-center text-gray-500 py-10">No analysis data available.</p>';

  targetContainer.innerHTML = wrappedContent;


  // Animate progress bars after render
  setTimeout(() => {
    const progressBars = targetContainer.querySelectorAll(".progress-fill");
    progressBars.forEach((el) => {
      el.style.width = el.dataset.target + "%";
    });
  }, 100);

  // Show "Load Last Call" button again after analysis is displayed
  const lastCallId = localStorage.getItem("lastCallId");
  const _llbEnd = document.getElementById("loadLastCallBtn");
  if (lastCallId && _llbEnd) {
    _llbEnd.classList.remove("hidden");
    _llbEnd.classList.add("flex");
  }
}

  window.ublCallAnalysis = {
    startAnalysisCountdown,
    fetchCallAnalysis,
    downloadAnalysis,
    downloadTranscript,
    openAnalysisModal,
    closeAnalysisModal,
    getCurrentCallId: () => currentAnalysisCallId,
  };
})();
