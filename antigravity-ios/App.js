import React, { useState, useEffect, useRef } from 'react';
import {
  StyleSheet,
  Text,
  View,
  TouchableOpacity,
  TextInput,
  ScrollView,
  FlatList,
  ActivityIndicator,
  SafeAreaView,
  StatusBar,
  Switch,
  Modal,
  Alert
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

export default function App() {
  const [activeTab, setActiveTab] = useState('chats'); // chats, files, tasks, console, settings
  const [serverUrl, setServerUrl] = useState('http://localhost:5000');
  const [apiKey, setApiKey] = useState('');
  const [connected, setConnected] = useState(false);
  const [conversations, setConversations] = useState([]);
  const [selectedConv, setSelectedConv] = useState(null);
  
  // Chats state
  const [messages, setMessages] = useState([]);
  const [chatInput, setChatInput] = useState('');
  const [loadingChats, setLoadingChats] = useState(false);

  // File Manager state
  const [currentPath, setCurrentPath] = useState('');
  const [files, setFiles] = useState([]);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [selectedFileContent, setSelectedFileContent] = useState(null);
  const [fileModalVisible, setFileModalVisible] = useState(false);
  const [gitDiffText, setGitDiffText] = useState('');
  const [diffModalVisible, setDiffModalVisible] = useState(false);

  // Tasks state
  const [tasks, setTasks] = useState([]);
  const [loadingTasks, setLoadingTasks] = useState(false);

  // Console state
  const [consoleInput, setConsoleInput] = useState('');
  const [consoleLogs, setConsoleLogs] = useState([]);
  const [executingCmd, setExecutingCmd] = useState(false);

  // Notifications state
  const [notifications, setNotifications] = useState([]);
  const [notifModalVisible, setNotifModalVisible] = useState(false);
  const [unreadNotifCount, setUnreadNotifCount] = useState(0);

  // Auto-reload intervals
  const pollTimerRef = useRef(null);

  // Common fetch helper
  const apiFetch = async (endpoint, options = {}) => {
    const url = `${serverUrl}${endpoint}`;
    const headers = {
      'Content-Type': 'application/json',
      'X-API-Key': apiKey,
      ...options.headers
    };
    try {
      const response = await fetch(url, { ...options, headers });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return await response.json();
    } catch (e) {
      console.warn(`Fetch error for ${endpoint}:`, e);
      throw e;
    }
  };

  // Test Connection
  const checkConnection = async () => {
    try {
      const data = await apiFetch('/api/agy/list');
      setConnected(true);
      setConversations(data.conversations || []);
      if (data.conversations && data.conversations.length > 0 && !selectedConv) {
        setSelectedConv(data.conversations[0].id);
      }
    } catch (e) {
      setConnected(false);
    }
  };

  // Fetch Conversations & Statuses
  useEffect(() => {
    checkConnection();
    const interval = setInterval(checkConnection, 10000);
    return () => clearInterval(interval);
  }, [serverUrl, apiKey]);

  // Load chat history for the selected conversation
  const loadChatHistory = async () => {
    if (!selectedConv) return;
    setLoadingChats(true);
    try {
      const data = await apiFetch(`/api/agy/transcript?conv_id=${selectedConv}`);
      if (data.success) {
        setMessages(data.transcript || []);
      }
    } catch (e) {
      // Handle fail
    } finally {
      setLoadingChats(false);
    }
  };

  // Load active tasks
  const loadTasksList = async () => {
    setLoadingTasks(true);
    try {
      const data = await apiFetch('/api/agy/status?conv_id=' + (selectedConv || ''));
      if (data.success) {
        setTasks(data.tasks || []);
      }
    } catch (e) {
      // Handle fail
    } finally {
      setLoadingTasks(false);
    }
  };

  // Load File list
  const loadFilesList = async (path = '') => {
    setLoadingFiles(true);
    try {
      const data = await apiFetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
      if (data.success) {
        setCurrentPath(data.current_path);
        setFiles(data.files || []);
      }
    } catch (e) {
      Alert.alert('Ошибка', 'Не удалось загрузить файлы');
    } finally {
      setLoadingFiles(false);
    }
  };

  // Poll chat updates, notifications, tasks
  useEffect(() => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    
    if (connected && selectedConv) {
      loadChatHistory();
      loadTasksList();
      
      pollTimerRef.current = setInterval(async () => {
        // Chat polling
        try {
          const chatData = await apiFetch(`/api/agy/transcript?conv_id=${selectedConv}`);
          if (chatData.success) {
            setMessages(chatData.transcript || []);
          }
          
          // Tasks polling
          const statusData = await apiFetch(`/api/agy/status?conv_id=${selectedConv}`);
          if (statusData.success) {
            setTasks(statusData.tasks || []);
          }
          
          // Notifications polling
          const notifData = await apiFetch('/api/agy/notifications');
          if (notifData.success) {
            setNotifications(notifData.notifications || []);
            const unread = notifData.notifications.filter(n => !n.read).length;
            setUnreadNotifCount(unread);
          }
        } catch (e) {
          // ignore
        }
      }, 3000);
    }
    
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, [connected, selectedConv]);

  // Sync files list when entering files tab
  useEffect(() => {
    if (activeTab === 'files' && connected) {
      loadFilesList(currentPath);
    }
  }, [activeTab, connected]);

  // Send Message to Agent
  const sendMessage = async () => {
    if (!chatInput.trim() || !selectedConv) return;
    const msgText = chatInput;
    setChatInput('');
    try {
      const data = await apiFetch('/api/agy/send', {
        method: 'POST',
        body: JSON.stringify({
          conv_id: selectedConv,
          message: msgText
        })
      });
      if (data.success) {
        loadChatHistory();
      } else {
        Alert.alert('Ошибка', 'Не удалось отправить сообщение');
      }
    } catch (e) {
      Alert.alert('Ошибка', 'Сервер недоступен');
    }
  };

  // Open file in modal
  const openFile = async (filePath) => {
    setLoadingFiles(true);
    try {
      const data = await apiFetch(`/api/fs/read?path=${encodeURIComponent(filePath)}`);
      if (data.success) {
        setSelectedFileContent({ path: filePath, content: data.content });
        setFileModalVisible(true);
      } else {
        Alert.alert('Ошибка', data.error || 'Не удалось открыть файл');
      }
    } catch (e) {
      Alert.alert('Ошибка', 'Сервер недоступен');
    } finally {
      setLoadingFiles(false);
    }
  };

  // Fetch Git Diff
  const showGitDiff = async () => {
    setLoadingFiles(true);
    try {
      const data = await apiFetch('/api/fs/diff');
      if (data.success) {
        setGitDiffText(data.diff || 'Нет незафиксированных изменений.');
        setDiffModalVisible(true);
      } else {
        Alert.alert('Ошибка', data.error || 'Не удалось получить Git Diff');
      }
    } catch (e) {
      Alert.alert('Ошибка', 'Сервер недоступен');
    } finally {
      setLoadingFiles(false);
    }
  };

  // Run Console command
  const runConsoleCommand = async () => {
    if (!consoleInput.trim()) return;
    const cmd = consoleInput;
    setConsoleInput('');
    setExecutingCmd(true);
    setConsoleLogs(prev => [...prev, { type: 'in', text: `> ${cmd}` }]);
    try {
      const data = await apiFetch('/api/fs/cmd', {
        method: 'POST',
        body: JSON.stringify({ cmd })
      });
      if (data.success) {
        setConsoleLogs(prev => [...prev, { type: 'out', text: data.stdout || data.stderr || 'Команда выполнена успешно (нет вывода)' }]);
      } else {
        setConsoleLogs(prev => [...prev, { type: 'err', text: data.error || 'Ошибка при выполнении' }]);
      }
    } catch (e) {
      setConsoleLogs(prev => [...prev, { type: 'err', text: 'Ошибка сети / сервер недоступен' }]);
    } finally {
      setExecutingCmd(false);
    }
  };

  // Clear notifications
  const clearNotifications = async () => {
    try {
      await apiFetch('/api/agy/notifications/clear', { method: 'POST' });
      setNotifications([]);
      setUnreadNotifCount(0);
    } catch (e) {
      // ignore
    }
  };

  // Render chat bubble items
  const renderMessageItem = ({ item }) => {
    // Detect roles/type
    const isUser = item.source === 'USER_EXPLICIT' || item.type === 'USER_INPUT';
    const isModel = item.source === 'MODEL' || item.type === 'PLANNER_RESPONSE';
    
    let content = item.content || '';
    if (typeof content !== 'string') {
      content = JSON.stringify(content);
    }
    
    if (!content.trim() && item.tool_calls) {
      content = `Вызов инструментов: ${item.tool_calls.map(tc => tc.name).join(', ')}`;
    }

    if (!content.trim()) return null;

    return (
      <View style={[styles.messageRow, isUser ? styles.messageRowUser : styles.messageRowAgent]}>
        {!isUser && (
          <View style={styles.agentAvatar}>
            <Ionicons name="flash" size={12} color="#fff" />
          </View>
        )}
        <View style={[styles.bubble, isUser ? styles.bubbleUser : styles.bubbleAgent]}>
          <Text style={[styles.bubbleText, isUser ? styles.bubbleTextUser : styles.bubbleTextAgent]}>
            {content}
          </Text>
          {item.tool_calls && item.tool_calls.length > 0 && (
            <View style={styles.toolList}>
              {item.tool_calls.map((tc, idx) => (
                <View key={idx} style={styles.toolBadge}>
                  <Ionicons name="construct" size={10} color="#0a84ff" />
                  <Text style={styles.toolBadgeText}>{tc.name}</Text>
                </View>
              ))}
            </View>
          )}
        </View>
      </View>
    );
  };

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#000" />
      
      {/* Header */}
      <View style={styles.header}>
        <View style={styles.headerLeft}>
          <Text style={styles.headerTitle}>Antigravity iOS</Text>
          <View style={[styles.statusIndicator, connected ? styles.statusOnline : styles.statusOffline]} />
        </View>
        
        <View style={styles.headerRight}>
          {conversations.length > 0 && (
            <View style={styles.pickerWrapper}>
              <Ionicons name="git-branch" size={16} color="#8e8e93" />
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ maxWidth: 120 }}>
                <TouchableOpacity 
                  style={styles.pickerSelector}
                  onPress={() => {
                    const ids = conversations.map(c => c.id);
                    Alert.alert('Выбор сессии', 'Выберите ID сессии агента:', 
                      ids.map(id => ({ text: id.substring(0,8) + '...', onPress: () => setSelectedConv(id) }))
                    );
                  }}
                >
                  <Text style={styles.pickerText} numberOfLines={1}>
                    {selectedConv ? selectedConv.substring(0, 8) + '...' : 'Выбрать'}
                  </Text>
                </TouchableOpacity>
              </ScrollView>
            </View>
          )}

          <TouchableOpacity style={styles.bellButton} onPress={() => setNotifModalVisible(true)}>
            <Ionicons name="notifications" size={24} color="#0a84ff" />
            {unreadNotifCount > 0 && (
              <View style={styles.bellBadge}>
                <Text style={styles.bellBadgeText}>{unreadNotifCount}</Text>
              </View>
            )}
          </TouchableOpacity>
        </View>
      </View>

      {/* Main Area */}
      <View style={styles.mainContent}>
        {!connected ? (
          <View style={styles.disconnectedPanel}>
            <Ionicons name="cloud-offline" size={64} color="#ff453a" />
            <Text style={styles.disconnectedTitle}>Нет соединения</Text>
            <Text style={styles.disconnectedText}>
              Проверь URL сервера в настройках и статус узла cloudflared (krnl-node).
            </Text>
            <TouchableOpacity style={styles.actionBtn} onPress={() => setActiveTab('settings')}>
              <Text style={styles.actionBtnText}>Перейти в настройки</Text>
            </TouchableOpacity>
          </View>
        ) : (
          <>
            {/* Chats Tab */}
            {activeTab === 'chats' && (
              <View style={styles.tabContent}>
                {loadingChats ? (
                  <View style={styles.centered}>
                    <ActivityIndicator size="large" color="#0a84ff" />
                  </View>
                ) : (
                  <FlatList
                    data={messages}
                    renderItem={renderMessageItem}
                    keyExtractor={(item, index) => index.toString()}
                    contentContainerStyle={styles.chatListContent}
                    ref={(ref) => { this.flatListRef = ref; }}
                    onContentSizeChange={() => this.flatListRef?.scrollToEnd({ animated: true })}
                  />
                )}
                {/* Chat Input */}
                <View style={styles.inputArea}>
                  <TextInput
                    style={styles.chatInput}
                    placeholder="Написать команду или сообщение..."
                    placeholderTextColor="#8e8e93"
                    value={chatInput}
                    onChangeText={setChatInput}
                  />
                  <TouchableOpacity style={styles.sendBtn} onPress={sendMessage}>
                    <Ionicons name="send" size={18} color="#fff" />
                  </TouchableOpacity>
                </View>
              </View>
            )}

            {/* File Manager Tab */}
            {activeTab === 'files' && (
              <View style={styles.tabContent}>
                <View style={styles.fileHeader}>
                  <Text style={styles.filePathText} numberOfLines={1}>
                    /{currentPath.split('/').slice(-2).join('/')}
                  </Text>
                  <View style={styles.fileHeaderActions}>
                    <TouchableOpacity style={styles.fileHeaderBtn} onPress={showGitDiff}>
                      <Ionicons name="git-compare" size={20} color="#0a84ff" />
                      <Text style={styles.fileHeaderBtnText}>Diff</Text>
                    </TouchableOpacity>
                    {currentPath !== '' && (
                      <TouchableOpacity style={styles.fileHeaderBtn} onPress={() => {
                        const parts = currentPath.split('/');
                        parts.pop();
                        loadFilesList(parts.join('/'));
                      }}>
                        <Ionicons name="arrow-up" size={20} color="#0a84ff" />
                      </TouchableOpacity>
                    )}
                  </View>
                </View>
                
                {loadingFiles ? (
                  <View style={styles.centered}>
                    <ActivityIndicator size="large" color="#0a84ff" />
                  </View>
                ) : (
                  <FlatList
                    data={files}
                    keyExtractor={(item) => item.name}
                    renderItem={({ item }) => (
                      <TouchableOpacity 
                        style={styles.fileItem}
                        onPress={() => item.isDir ? loadFilesList(item.path) : openFile(item.path)}
                      >
                        <Ionicons 
                          name={item.isDir ? "folder" : "document-text"} 
                          size={24} 
                          color={item.isDir ? "#ff9500" : "#8e8e93"} 
                        />
                        <View style={styles.fileItemTextWrapper}>
                          <Text style={styles.fileItemName} numberOfLines={1}>{item.name}</Text>
                          {!item.isDir && <Text style={styles.fileItemSize}>{(item.sizeBytes / 1024).toFixed(1)} KB</Text>}
                        </View>
                        <Ionicons name="chevron-forward" size={16} color="#48484a" />
                      </TouchableOpacity>
                    )}
                  />
                )}
              </View>
            )}

            {/* Tasks Tab */}
            {activeTab === 'tasks' && (
              <View style={styles.tabContent}>
                <View style={styles.sectionHeader}>
                  <Text style={styles.sectionHeaderTitle}>Активные процессы</Text>
                  <TouchableOpacity onPress={loadTasksList}>
                    <Ionicons name="refresh" size={20} color="#0a84ff" />
                  </TouchableOpacity>
                </View>
                
                {loadingTasks ? (
                  <View style={styles.centered}>
                    <ActivityIndicator size="large" color="#0a84ff" />
                  </View>
                ) : tasks.length === 0 ? (
                  <View style={styles.emptyPanel}>
                    <Ionicons name="checkmark-circle-outline" size={48} color="#30d158" />
                    <Text style={styles.emptyPanelText}>Все задачи агента завершены</Text>
                  </View>
                ) : (
                  <FlatList
                    data={tasks}
                    keyExtractor={(item) => item.id}
                    renderItem={({ item }) => (
                      <View style={styles.taskCard}>
                        <View style={styles.taskCardHeader}>
                          <Text style={styles.taskCardTitle}>{item.name || 'Процесс'}</Text>
                          <View style={styles.taskStatusPill}>
                            <Text style={styles.taskStatusText}>{item.status}</Text>
                          </View>
                        </View>
                        <Text style={styles.taskCardDesc} numberOfLines={2}>{item.cmd}</Text>
                        {item.start_time && (
                          <Text style={styles.taskCardTime}>Запущен: {item.start_time}</Text>
                        )}
                        <View style={styles.taskCardActions}>
                          <TouchableOpacity 
                            style={styles.taskActionBtnKill}
                            onPress={async () => {
                              try {
                                await apiFetch(`/api/agy/task/kill?id=${item.id}`, { method: 'POST' });
                                loadTasksList();
                              } catch(e) {
                                Alert.alert('Ошибка', 'Не удалось остановить процесс');
                              }
                            }}
                          >
                            <Text style={styles.taskActionBtnText}>Остановить</Text>
                          </TouchableOpacity>
                        </View>
                      </View>
                    )}
                  />
                )}
              </View>
            )}

            {/* Console Tab */}
            {activeTab === 'console' && (
              <View style={styles.tabContent}>
                <View style={styles.consoleHeader}>
                  <Text style={styles.consoleHeaderTitle}>Shell консоль ПК</Text>
                  <TouchableOpacity onPress={() => setConsoleLogs([])}>
                    <Ionicons name="trash" size={18} color="#ff453a" />
                  </TouchableOpacity>
                </View>
                
                <ScrollView 
                  style={styles.consoleLogWrapper}
                  ref={ref => { this.consoleScroll = ref; }}
                  onContentSizeChange={() => this.consoleScroll?.scrollToEnd({ animated: true })}
                >
                  {consoleLogs.map((log, index) => (
                    <Text 
                      key={index} 
                      style={[
                        styles.consoleLogText, 
                        log.type === 'in' && styles.consoleLogTextIn,
                        log.type === 'err' && styles.consoleLogTextErr
                      ]}
                    >
                      {log.text}
                    </Text>
                  ))}
                  {executingCmd && (
                    <ActivityIndicator size="small" color="#0a84ff" style={{ marginVertical: 10 }} />
                  )}
                </ScrollView>

                <View style={styles.inputArea}>
                  <TextInput
                    style={styles.consoleInput}
                    placeholder="Напиши команду ПК (e.g. dir, git status)..."
                    placeholderTextColor="#8e8e93"
                    value={consoleInput}
                    onChangeText={setConsoleInput}
                  />
                  <TouchableOpacity style={styles.sendBtn} onPress={runConsoleCommand}>
                    <Ionicons name="play" size={18} color="#fff" />
                  </TouchableOpacity>
                </View>
              </View>
            )}
          </>
        )}

        {/* Settings Tab (Always loaded) */}
        {activeTab === 'settings' && (
          <ScrollView style={styles.settingsContent}>
            <Text style={styles.settingsSectionTitle}>Подключение к ПК</Text>
            
            <View style={styles.settingsGroup}>
              <View style={styles.settingsRow}>
                <Text style={styles.settingsLabel}>Адрес сервера:</Text>
                <TextInput
                  style={styles.settingsInput}
                  value={serverUrl}
                  onChangeText={setServerUrl}
                  placeholder="e.g. http://192.168.1.50:5000"
                  placeholderTextColor="#48484a"
                />
              </View>
              <View style={styles.settingsRow}>
                <Text style={styles.settingsLabel}>API ключ:</Text>
                <TextInput
                  style={styles.settingsInput}
                  value={apiKey}
                  onChangeText={setApiKey}
                  placeholder="Токен (X-API-Key)"
                  placeholderTextColor="#48484a"
                  secureTextEntry
                />
              </View>
            </View>

            <Text style={styles.settingsSectionTitle}>Настройки уведомлений</Text>
            <View style={styles.settingsGroup}>
              <View style={styles.settingsRowSwitch}>
                <Text style={styles.settingsLabel}>Пуши при завершении агента</Text>
                <Switch value={true} onValueChange={() => {}} trackColor={{ true: '#30d158' }} />
              </View>
            </View>

            <TouchableOpacity style={styles.testBtn} onPress={checkConnection}>
              <Text style={styles.testBtnText}>Проверить соединение</Text>
            </TouchableOpacity>

            <Text style={styles.versionText}>Connect Antigravity Companion v1.0.0</Text>
          </ScrollView>
        )}
      </View>

      {/* Navigation Tabs Bar */}
      <View style={styles.tabBar}>
        <TouchableOpacity 
          style={[styles.tabItem, activeTab === 'chats' && styles.tabItemActive]} 
          onPress={() => setActiveTab('chats')}
        >
          <Ionicons name="chatbubble" size={24} color={activeTab === 'chats' ? '#0a84ff' : '#8e8e93'} />
          <Text style={[styles.tabItemText, activeTab === 'chats' && styles.tabItemTextActive]}>Чаты</Text>
        </TouchableOpacity>

        <TouchableOpacity 
          style={[styles.tabItem, activeTab === 'files' && styles.tabItemActive]} 
          onPress={() => setActiveTab('files')}
        >
          <Ionicons name="folder-open" size={24} color={activeTab === 'files' ? '#0a84ff' : '#8e8e93'} />
          <Text style={[styles.tabItemText, activeTab === 'files' && styles.tabItemTextActive]}>Файлы</Text>
        </TouchableOpacity>

        <TouchableOpacity 
          style={[styles.tabItem, activeTab === 'tasks' && styles.tabItemActive]} 
          onPress={() => setActiveTab('tasks')}
        >
          <Ionicons name="list-circle" size={24} color={activeTab === 'tasks' ? '#0a84ff' : '#8e8e93'} />
          <Text style={[styles.tabItemText, activeTab === 'tasks' && styles.tabItemTextActive]}>Задачи</Text>
        </TouchableOpacity>

        <TouchableOpacity 
          style={[styles.tabItem, activeTab === 'console' && styles.tabItemActive]} 
          onPress={() => setActiveTab('console')}
        >
          <Ionicons name="terminal" size={24} color={activeTab === 'console' ? '#0a84ff' : '#8e8e93'} />
          <Text style={[styles.tabItemText, activeTab === 'console' && styles.tabItemTextActive]}>Консоль</Text>
        </TouchableOpacity>

        <TouchableOpacity 
          style={[styles.tabItem, activeTab === 'settings' && styles.tabItemActive]} 
          onPress={() => setActiveTab('settings')}
        >
          <Ionicons name="settings" size={24} color={activeTab === 'settings' ? '#0a84ff' : '#8e8e93'} />
          <Text style={[styles.tabItemText, activeTab === 'settings' && styles.tabItemTextActive]}>Настройки</Text>
        </TouchableOpacity>
      </View>

      {/* Notifications Modal */}
      <Modal visible={notifModalVisible} animationType="slide" transparent={true}>
        <View style={styles.modalOverlay}>
          <View style={styles.modalContent}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>Уведомления</Text>
              <TouchableOpacity onPress={() => setNotifModalVisible(false)}>
                <Ionicons name="close" size={24} color="#8e8e93" />
              </TouchableOpacity>
            </View>
            {notifications.length === 0 ? (
              <View style={styles.centeredModal}>
                <Ionicons name="notifications-off" size={48} color="#48484a" />
                <Text style={styles.noNotifText}>Нет новых уведомлений</Text>
              </View>
            ) : (
              <FlatList
                data={notifications}
                keyExtractor={(item, index) => index.toString()}
                renderItem={({ item }) => (
                  <View style={[styles.notifItem, !item.read && styles.notifItemUnread]}>
                    <Text style={styles.notifTime}>{item.timestamp}</Text>
                    <Text style={styles.notifText}>{item.message}</Text>
                  </View>
                )}
              />
            )}
            <TouchableOpacity style={styles.clearNotifBtn} onPress={clearNotifications}>
              <Text style={styles.clearNotifText}>Очистить историю</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>

      {/* File Viewer Modal */}
      <Modal visible={fileModalVisible} animationType="slide" transparent={true}>
        <View style={styles.modalOverlay}>
          <View style={styles.modalContentLarge}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle} numberOfLines={1}>
                {selectedFileContent?.path.split('/').pop() || 'Просмотр файла'}
              </Text>
              <TouchableOpacity onPress={() => setFileModalVisible(false)}>
                <Ionicons name="close" size={24} color="#8e8e93" />
              </TouchableOpacity>
            </View>
            <ScrollView style={styles.fileViewerBody}>
              <Text style={styles.fileContentText}>{selectedFileContent?.content}</Text>
            </ScrollView>
          </View>
        </View>
      </Modal>

      {/* Git Diff Modal */}
      <Modal visible={diffModalVisible} animationType="slide" transparent={true}>
        <View style={styles.modalOverlay}>
          <View style={styles.modalContentLarge}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>Git Diff изменений</Text>
              <TouchableOpacity onPress={() => setDiffModalVisible(false)}>
                <Ionicons name="close" size={24} color="#8e8e93" />
              </TouchableOpacity>
            </View>
            <ScrollView style={styles.fileViewerBody}>
              <Text style={styles.diffContentText}>{gitDiffText}</Text>
            </ScrollView>
          </View>
        </View>
      </Modal>

    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#000',
    fontFamily: 'System'
  },
  header: {
    height: 52,
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e',
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    backgroundColor: '#161618'
  },
  headerLeft: {
    flexDirection: 'row',
    alignItems: 'center'
  },
  headerTitle: {
    fontSize: 20,
    fontWeight: '700',
    color: '#fff',
    letterSpacing: -0.5
  },
  statusIndicator: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginLeft: 8
  },
  statusOnline: {
    backgroundColor: '#30d158'
  },
  statusOffline: {
    backgroundColor: '#ff453a'
  },
  headerRight: {
    flexDirection: 'row',
    alignItems: 'center'
  },
  pickerWrapper: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#2c2c2e',
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 8,
    marginRight: 12
  },
  pickerSelector: {
    marginLeft: 6
  },
  pickerText: {
    color: '#ececf2',
    fontSize: 12,
    fontWeight: '600'
  },
  bellButton: {
    position: 'relative',
    padding: 2
  },
  bellBadge: {
    position: 'absolute',
    top: -2,
    right: -2,
    backgroundColor: '#ff453a',
    borderRadius: 8,
    minWidth: 16,
    height: 16,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 3
  },
  bellBadgeText: {
    color: '#fff',
    fontSize: 10,
    fontWeight: '800'
  },
  mainContent: {
    flex: 1,
    backgroundColor: '#000'
  },
  tabContent: {
    flex: 1
  },
  disconnectedPanel: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 32
  },
  disconnectedTitle: {
    color: '#fff',
    fontSize: 22,
    fontWeight: '700',
    marginTop: 16,
    marginBottom: 8
  },
  disconnectedText: {
    color: '#8e8e93',
    textAlign: 'center',
    fontSize: 14,
    lineHeight: 20,
    marginBottom: 24
  },
  actionBtn: {
    backgroundColor: '#0a84ff',
    paddingVertical: 12,
    paddingHorizontal: 24,
    borderRadius: 12
  },
  actionBtnText: {
    color: '#fff',
    fontWeight: '600',
    fontSize: 15
  },
  centered: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center'
  },
  chatListContent: {
    paddingHorizontal: 12,
    paddingVertical: 16,
    paddingBottom: 24
  },
  messageRow: {
    flexDirection: 'row',
    marginVertical: 6,
    alignItems: 'flex-end',
    maxWidth: '85%'
  },
  messageRowUser: {
    alignSelf: 'flex-end'
  },
  messageRowAgent: {
    alignSelf: 'flex-start'
  },
  agentAvatar: {
    width: 22,
    height: 22,
    borderRadius: 11,
    backgroundColor: '#0a84ff',
    alignItems: 'center',
    justifyContent: 'center',
    marginRight: 6,
    marginBottom: 4
  },
  bubble: {
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 18
  },
  bubbleUser: {
    backgroundColor: '#0a84ff',
    borderBottomRightRadius: 2
  },
  bubbleAgent: {
    backgroundColor: '#1c1c1e',
    borderBottomLeftRadius: 2
  },
  bubbleText: {
    fontSize: 15,
    lineHeight: 20
  },
  bubbleTextUser: {
    color: '#fff'
  },
  bubbleTextAgent: {
    color: '#ececf2'
  },
  toolList: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    marginTop: 6,
    gap: 4
  },
  toolBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#2c2c2e',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 6
  },
  toolBadgeText: {
    color: '#0a84ff',
    fontSize: 10,
    fontWeight: '700',
    marginLeft: 3
  },
  inputArea: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 12,
    borderTopWidth: 0.5,
    borderTopColor: '#2c2c2e',
    backgroundColor: '#161618'
  },
  chatInput: {
    flex: 1,
    backgroundColor: '#2c2c2e',
    color: '#fff',
    borderRadius: 20,
    paddingHorizontal: 16,
    paddingVertical: 8,
    fontSize: 15,
    maxHeight: 80
  },
  sendBtn: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: '#0a84ff',
    alignItems: 'center',
    justifyContent: 'center',
    marginLeft: 8
  },
  fileHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e',
    backgroundColor: '#161618'
  },
  fileHeaderActions: {
    flexDirection: 'row',
    gap: 12,
    alignItems: 'center'
  },
  fileHeaderBtn: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: '#2c2c2e',
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 8
  },
  fileHeaderBtnText: {
    color: '#0a84ff',
    fontSize: 13,
    fontWeight: '600',
    marginLeft: 4
  },
  filePathText: {
    color: '#8e8e93',
    fontSize: 13,
    fontWeight: '600',
    flex: 1
  },
  fileItem: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 12,
    paddingHorizontal: 16,
    borderBottomWidth: 0.5,
    borderBottomColor: '#1c1c1e'
  },
  fileItemTextWrapper: {
    flex: 1,
    marginLeft: 12
  },
  fileItemName: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '500'
  },
  fileItemSize: {
    color: '#8e8e93',
    fontSize: 11,
    marginTop: 2
  },
  sectionHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: '#161618',
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e'
  },
  sectionHeaderTitle: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '700'
  },
  emptyPanel: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 32
  },
  emptyPanelText: {
    color: '#8e8e93',
    fontSize: 15,
    marginTop: 12
  },
  taskCard: {
    backgroundColor: '#1c1c1e',
    borderRadius: 14,
    marginHorizontal: 16,
    marginVertical: 8,
    padding: 16
  },
  taskCardHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 8
  },
  taskCardTitle: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '700'
  },
  taskStatusPill: {
    backgroundColor: '#30d15822',
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 6
  },
  taskStatusText: {
    color: '#30d158',
    fontSize: 11,
    fontWeight: '700'
  },
  taskCardDesc: {
    color: '#8e8e93',
    fontSize: 13,
    lineHeight: 18,
    marginBottom: 8
  },
  taskCardTime: {
    color: '#48484a',
    fontSize: 11,
    marginBottom: 12
  },
  taskCardActions: {
    alignItems: 'flex-end'
  },
  taskActionBtnKill: {
    backgroundColor: '#ff453a22',
    paddingVertical: 6,
    paddingHorizontal: 12,
    borderRadius: 8
  },
  taskActionBtnText: {
    color: '#ff453a',
    fontSize: 12,
    fontWeight: '600'
  },
  consoleHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: '#161618',
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e'
  },
  consoleHeaderTitle: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '700'
  },
  consoleLogWrapper: {
    flex: 1,
    backgroundColor: '#0a0a0c',
    padding: 16
  },
  consoleLogText: {
    fontFamily: 'monospace',
    fontSize: 12,
    color: '#30d158',
    marginVertical: 2,
    lineHeight: 16
  },
  consoleLogTextIn: {
    color: '#fff',
    fontWeight: '700'
  },
  consoleLogTextErr: {
    color: '#ff453a'
  },
  consoleInput: {
    flex: 1,
    backgroundColor: '#2c2c2e',
    color: '#fff',
    borderRadius: 20,
    paddingHorizontal: 16,
    paddingVertical: 8,
    fontSize: 13,
    fontFamily: 'monospace'
  },
  settingsContent: {
    flex: 1,
    padding: 16
  },
  settingsSectionTitle: {
    color: '#8e8e93',
    fontSize: 13,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
    marginBottom: 8,
    marginTop: 16,
    marginLeft: 4
  },
  settingsGroup: {
    backgroundColor: '#1c1c1e',
    borderRadius: 12,
    overflow: 'hidden',
    marginBottom: 16
  },
  settingsRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 12,
    paddingHorizontal: 16,
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e'
  },
  settingsRowSwitch: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: 8,
    paddingHorizontal: 16
  },
  settingsLabel: {
    color: '#fff',
    fontSize: 15,
    fontWeight: '500'
  },
  settingsInput: {
    flex: 1,
    textAlign: 'right',
    color: '#fff',
    fontSize: 15,
    marginLeft: 16
  },
  testBtn: {
    backgroundColor: '#0a84ff',
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 12,
    shadowColor: '#0a84ff',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.2,
    shadowRadius: 8
  },
  testBtnText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '700'
  },
  versionText: {
    color: '#48484a',
    textAlign: 'center',
    fontSize: 11,
    marginTop: 32,
    marginBottom: 32
  },
  tabBar: {
    height: 56,
    borderTopWidth: 0.5,
    borderTopColor: '#2c2c2e',
    backgroundColor: '#161618',
    flexDirection: 'row',
    justifyContent: 'space-around',
    alignItems: 'center',
    paddingBottom: 4
  },
  tabItem: {
    alignItems: 'center',
    justifyContent: 'center',
    flex: 1
  },
  tabItemActive: {},
  tabItemText: {
    fontSize: 10,
    color: '#8e8e93',
    marginTop: 4,
    fontWeight: '600'
  },
  tabItemTextActive: {
    color: '#0a84ff'
  },
  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'flex-end'
  },
  modalContent: {
    backgroundColor: '#1c1c1e',
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    height: '60%',
    padding: 16
  },
  modalContentLarge: {
    backgroundColor: '#1c1c1e',
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    height: '85%',
    padding: 16
  },
  modalHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 16,
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e',
    paddingBottom: 12
  },
  modalTitle: {
    color: '#fff',
    fontSize: 18,
    fontWeight: '700',
    maxWidth: '80%'
  },
  centeredModal: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center'
  },
  noNotifText: {
    color: '#8e8e93',
    fontSize: 14,
    marginTop: 8
  },
  notifItem: {
    padding: 12,
    borderBottomWidth: 0.5,
    borderBottomColor: '#2c2c2e'
  },
  notifItemUnread: {
    backgroundColor: '#0a84ff11'
  },
  notifTime: {
    color: '#8e8e93',
    fontSize: 10,
    marginBottom: 4
  },
  notifText: {
    color: '#fff',
    fontSize: 13,
    lineHeight: 18
  },
  clearNotifBtn: {
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 12,
    marginTop: 8
  },
  clearNotifText: {
    color: '#ff453a',
    fontWeight: '600',
    fontSize: 14
  },
  fileViewerBody: {
    flex: 1
  },
  fileContentText: {
    fontFamily: 'monospace',
    color: '#fff',
    fontSize: 12,
    lineHeight: 16
  },
  diffContentText: {
    fontFamily: 'monospace',
    color: '#ff9500',
    fontSize: 11,
    lineHeight: 15
  }
});
